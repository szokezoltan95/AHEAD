"""Local, read-only telemetry and draft/apply configuration over HTTP."""

from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
from pathlib import Path
from threading import Event, Lock, Thread
from urllib.parse import urlsplit

from ahead.simulator.floorplan import WALL_TYPES
from ahead.simulator.presets import PRESETS
from ahead.simulator.project import SETTINGS, compile_project, prepare_project, validate_draft


class Console:
    def __init__(self, path: Path):
        self.path = path
        self.lock = Lock()
        self.changed = Event()
        self.example = json.loads(files(__package__).joinpath("example.json").read_text("utf-8"))
        self.draft = self.example
        self.applied = None
        self.pending = None
        self.initial_state = None
        self.snapshot = {"status": "waiting for simulator"}
        if path.exists():
            saved = json.loads(path.read_text("utf-8"))
            self.draft = saved["draft"]
            self.applied = saved["applied"]
            self.initial_state = saved.get("initial_state")
            if self.applied is not None:
                if "initial_state" in saved:
                    self.pending = compile_project(self.applied)
                    self.pending[0].house.restore_checkpoint(saved["initial_state"])
                else:
                    self.pending = prepare_project(self.applied)

    def next_run(self):
        with self.lock:
            pending = self.pending
            self.pending = None
            self.changed.clear()
            return pending

    def update(self, project, apply):
        if apply:
            prepared = prepare_project(project)
        else:
            validate_draft(project)
        with self.lock:
            applied = project if apply else self.applied
            checkpoint = prepared[0].house.checkpoint() if apply else self.initial_state
            data = {"draft": project, "applied": applied}
            if checkpoint is not None:
                data["initial_state"] = checkpoint
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp = self.path.with_suffix(".tmp")
            temp.write_text(json.dumps(data, indent=2, allow_nan=False) + "\n", encoding="utf-8")
            temp.replace(self.path)
            self.draft = project
            if apply:
                self.applied = project
                self.initial_state = checkpoint
                self.pending = prepared
                self.snapshot = {"status": "applying project"}
                self.changed.set()

    def record(self, scenario, sample, diagnostic):
        state = scenario.house.diagnostics()
        for name, room in state["rooms"].items():
            zone = scenario.house.zones[name]
            room["reported_c"] = sample.observation.indoor_c[name] if zone.expose_sensor else None
            room["reported_to_ha"] = zone.expose_sensor
            room["target_c"] = zone.setpoint_c
        if diagnostic:
            for surface in diagnostic["surfaces"].values():
                inside = state["rooms"][surface["room"]]
                boundary = surface["boundary"]
                if boundary == "room":
                    first = inside["air_c"]
                    second = state["rooms"][surface["adjacent_room"]]["air_c"]
                else:
                    first = inside["fabric_c"]
                    second = (sample.actual_outdoor_c if boundary == "outdoors" else
                              diagnostic.get("ground_c", first) if boundary == "ground" else first)
                surface["inside_node_c"] = first
                surface["adjacent_node_c"] = second
                surface["heat_into_room_kw"] = (second - first) * surface["model_conductance_kw_k"]
            for name, value in state["balance"].get("solar_collector_kw", {}).items():
                diagnostic["solar"][name]["delivered_kw"] = value
        result = {
            "status": "running", "elapsed_s": sample.end_s,
            "weather": {"actual_outdoor_c": sample.actual_outdoor_c,
                        "reported_outdoor_c": sample.observation.outdoor_c,
                        "forecast_outdoor_c": sample.observation.forecast_outdoor_c,
                        "electricity_price": sample.observation.electricity_price_kwh,
                        "gas_price": sample.observation.gas_price_kwh},
            "active_faults": [
                asdict(fault) for fault in scenario.faults if fault.active(sample.start_s)
            ],
            "totals": asdict(scenario.house.totals),
            **state,
            **(diagnostic or {}),
        }
        encoded = json.dumps(result, allow_nan=False).encode()
        with self.lock:
            if not self.changed.is_set():
                self.snapshot = encoded


def serve_console(console: Console, port: int) -> ThreadingHTTPServer:
    resources = files(__package__)
    assets = {
        "/": (resources.joinpath("console.html").read_bytes(), "text/html; charset=utf-8"),
        "/assets/console.css": (resources.joinpath("console.css").read_bytes(), "text/css; charset=utf-8"),
        "/assets/floorplan.js": (resources.joinpath("floorplan.js").read_bytes(), "text/javascript; charset=utf-8"),
        "/assets/live-view.js": (resources.joinpath("live-view.js").read_bytes(), "text/javascript; charset=utf-8"),
        "/assets/system-views.js": (resources.joinpath("system-views.js").read_bytes(), "text/javascript; charset=utf-8"),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, payload, status=200, mime="application/json"):
            if not isinstance(payload, bytes):
                payload = json.dumps(payload, allow_nan=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(payload)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(payload)

        def local_request(self):
            try:
                host = urlsplit("//" + self.headers.get("Host", "")).hostname
            except ValueError:
                host = None
            if host in {"localhost", "127.0.0.1", "::1"}:
                return True
            self.send({"error": "Access this local console via localhost"}, 403)
            return False

        def do_GET(self):
            if not self.local_request():
                return
            path = urlsplit(self.path).path
            if path in assets:
                payload, mime = assets[path]
                self.send(payload, mime=mime)
                return
            match path:
                case "/api/live":
                    with console.lock:
                        snapshot = console.snapshot
                    self.send(snapshot)
                case "/api/project":
                    with console.lock:
                        data = {"draft": console.draft, "applied": console.applied,
                                "initial_state": console.initial_state}
                    self.send(data)
                case "/api/catalog":
                    self.send({"presets": PRESETS, "settings": SETTINGS, "example": console.example,
                               "wall_types": list(WALL_TYPES)})
                case _:
                    self.send({"error": "Unknown endpoint"}, 404)

        def do_POST(self):
            if not self.local_request():
                return
            route = urlsplit(self.path).path
            if route not in ("/api/project", "/api/apply"):
                self.send({"error": "Unknown endpoint"}, 404)
                return
            if self.headers.get_content_type() != "application/json":
                self.send({"error": "Use application/json"}, 415)
                return
            origin = self.headers.get("Origin")
            if origin and urlsplit(origin).netloc != self.headers.get("Host"):
                self.send({"error": "Origin does not match this console"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1_000_000:
                    raise ValueError("Configuration body must be between 1 byte and 1 MB")
                project = json.loads(self.rfile.read(length))
                console.update(project, route == "/api/apply")
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                self.send({"error": str(error)}, 400)
                return
            except OSError:
                self.send({"error": "Could not persist the configuration"}, 500)
                return
            self.send({"status": "saved" if route == "/api/project" else "applying"})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server

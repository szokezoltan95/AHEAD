"""Local telemetry, device controls, and project configuration over HTTP."""

from dataclasses import asdict
from datetime import datetime, timezone
import gzip
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
    def __init__(self, path: Path, default_project: dict | None = None):
        self.path = path
        self.lock = Lock()
        self.changed = Event()
        self.example = (default_project if default_project is not None
                        else json.loads(files(__package__).joinpath("example.json").read_text("utf-8")))
        self.draft = self.example
        self.applied = None
        self.pending = None
        self.initial_state = None
        self.remote_devices = {}
        self.remote_callback = None
        self.snapshot = {"status": "waiting for simulator"}
        self.history_rows = []
        self.history_last_s = -60
        self.logger = TelemetryLogger(path.parent / "logs")
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

    def bind_remote(self, devices, callback):
        with self.lock:
            self.remote_devices = devices
            self.remote_callback = callback

    def start_logging(self, interval_s):
        if type(interval_s) is not int or interval_s not in {1, 2, 5, 10, 30, 60}:
            raise ValueError("Choose a logging interval of 1, 2, 5, 10, 30 or 60 seconds")
        with self.lock:
            initial = self.snapshot
            if isinstance(initial, bytes):
                initial = json.loads(initial)
            self.logger.start(interval_s, self.applied, initial)

    def stop_logging(self):
        return self.logger.stop()

    def remote_command(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"device", "field", "value"}:
            raise ValueError("Expected device, field, and value")
        with self.lock:
            if self.remote_callback is None or self.changed.is_set():
                raise ValueError("Device controls are unavailable while the simulator starts")
            self.remote_callback(**payload)

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
        climates = sample.plan.climate_states
        remote_state = {}
        state_fields = {"target": "target_c", "sensor_source": "selected_sensor_source",
                        "external_temperature": "external_c", "sensor_offset": "sensor_offset_c",
                        "overshoot": "overshoot_correction_c"}
        for key, unit in climates.items():
            if key in self.remote_devices:
                remote_state[key] = {
                    field: unit.get(state_fields.get(field, field))
                    for field in self.remote_devices[key]["fields"]
                }
            group = "emitters" if unit["kind"] == "trv" else "sources"
            if unit["device"] in state[group]:
                # The physics diagnostics are shared with the simulator, so copy
                # the selected entry before adding controller-specific fields.
                state[group] = state[group] | {unit["device"]:state[group][unit["device"]] | {"controller" if unit["kind"] == "hydronic" else "thermostat": unit}}
        if sample.end_s - self.history_last_s >= 60:
            values = {f"room:{n}": v["air_c"] for n,v in state["rooms"].items()}
            values.update({f"water:{n}":v.get("supply_c") for n,v in state["circuits"].items()})
            values.update({f"boiler:{n}":v.get("heat_kw",0) for n,v in state["sources"].items()
                           if v.get("fuel")=="gas"})
            values.update({f"ac:{n}":v.get("heat_kw",0) for n,v in state["sources"].items()
                           if v.get("outdoor_unit")})
            values.update({f"trv:{n}":v.get("opening",0)*100 for n,v in state["emitters"].items()})
            values.update({f"pump:{n}":float(v.get("pump_on",False)) for n,v in state["sources"].items()
                           if v.get("fuel")=="gas"})
            values.update({f"boiler-output:{n}":v.get("heat_kw",0) for n,v in state["sources"].items()
                           if v.get("fuel")=="gas"})
            values.update({f"room-rh:{n}":v.get("humidity_pct") for n,v in state["rooms"].items()})
            self.history_rows.append({"t":sample.end_s,"v":values})
            self.history_rows = self.history_rows[-10080:]
            self.history_last_s = sample.end_s
        result = {
            "status": "running", "elapsed_s": sample.end_s,
            "remote_devices": self.remote_devices, "remote_state": remote_state,
            "weather": {"actual_outdoor_c": sample.actual_outdoor_c,
                        "reported_outdoor_c": sample.observation.outdoor_c,
                        "forecast_outdoor_c": sample.observation.forecast_outdoor_c,
                        "electricity_price": sample.observation.electricity_price_kwh,
                        "gas_price": sample.observation.gas_price_kwh,
                        "heat_pump_price": sample.observation.heat_pump_price_kwh},
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
                self.logger.record(sample.end_s, encoded)


class TelemetryLogger:
    """Write detailed simulator snapshots to a downloadable gzip JSONL trace."""

    def __init__(self, directory: Path):
        self.directory = directory
        self.lock = Lock()
        self.stream = None
        self.path = None
        self.interval_s = None
        self.next_sample_s = None
        self.samples = 0
        self.error = None

    def start(self, interval_s, project, initial_state):
        with self.lock:
            if self.stream is not None:
                raise ValueError("A detailed log is already recording")
            self.directory.mkdir(parents=True, exist_ok=True)
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            self.path = self.directory / f"ahead-simulator-{stamp}.jsonl.gz"
            self.stream = gzip.open(self.path, "wt", encoding="utf-8", newline="\n")
            self.interval_s = interval_s
            self.next_sample_s = 0
            self.samples = 0
            self.error = None
            header = {"type": "metadata", "format": "ahead-simulator-jsonl-v1",
                      "started_utc": datetime.now(timezone.utc).isoformat(),
                      "sample_interval_s": interval_s, "project": project,
                      "initial_state": initial_state}
            self.stream.write(json.dumps(header, allow_nan=False, separators=(",", ":")) + "\n")

    def record(self, elapsed_s, encoded_snapshot):
        with self.lock:
            if self.stream is None or elapsed_s + 1e-9 < self.next_sample_s:
                return
            try:
                self.stream.write('{"type":"sample","elapsed_s":')
                self.stream.write(f"{elapsed_s:.3f},\"state\":")
                self.stream.write(encoded_snapshot.decode("utf-8"))
                self.stream.write("}\n")
                self.stream.flush()
                self.samples += 1
                self.next_sample_s = elapsed_s + self.interval_s
            except (OSError, ValueError) as error:
                self.error = str(error)
                self.stream.close()
                self.stream = None

    def stop(self):
        with self.lock:
            was_active = self.stream is not None
            if self.stream is not None:
                self.stream.close()
                self.stream = None
            return was_active

    def status(self):
        with self.lock:
            return {"active": self.stream is not None,
                    "interval_s": self.interval_s, "samples": self.samples,
                    "file": self.path.name if self.path else None,
                    "ready": bool(self.path and self.stream is None and self.path.exists()),
                    "error": self.error}

    def due(self, elapsed_s):
        with self.lock:
            if self.stream is not None and self.next_sample_s is not None and elapsed_s < self.next_sample_s - self.interval_s:
                self.next_sample_s = elapsed_s
            return self.stream is not None and elapsed_s + 1e-9 >= self.next_sample_s

    def download_path(self):
        with self.lock:
            if self.stream is not None:
                raise ValueError("Stop the recording before downloading it")
            if not self.path or not self.path.exists():
                raise ValueError("There is no completed detailed log to download")
            return self.path


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
                case "/api/history":
                    with console.lock:
                        rows=list(console.history_rows)
                    signals={}
                    if rows:
                        for key in rows[0]["v"]:
                            signals[key]=key.replace(":"," · ").replace("_"," ").title()
                    self.send({"rows":rows,"signals":signals})
                case "/api/live":
                    with console.lock:
                        snapshot = console.snapshot
                    self.send(snapshot)
                case "/api/logger":
                    self.send(console.logger.status())
                case "/api/logger/download":
                    try:
                        path = console.logger.download_path()
                    except ValueError as error:
                        self.send({"error": str(error)}, 409)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Length", str(path.stat().st_size))
                    self.send_header("Content-Disposition", f'attachment; filename="{path.name}"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    with path.open("rb") as stream:
                        while chunk := stream.read(1024 * 1024):
                            self.wfile.write(chunk)
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
            if route not in ("/api/project", "/api/apply", "/api/remote",
                             "/api/logger/start", "/api/logger/stop"):
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
                payload = json.loads(self.rfile.read(length))
                if route == "/api/remote":
                    console.remote_command(payload)
                elif route == "/api/logger/start":
                    if not isinstance(payload, dict) or set(payload) != {"interval_s"}:
                        raise ValueError("Choose a logging interval")
                    console.start_logging(payload["interval_s"])
                elif route == "/api/logger/stop":
                    console.stop_logging()
                else:
                    console.update(payload, route == "/api/apply")
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                self.send({"error": str(error)}, 400)
                return
            except OSError:
                self.send({"error": "Could not persist the configuration"}, 500)
                return
            self.send({"status": {"/api/project": "saved", "/api/apply": "applying",
                                  "/api/remote": "accepted", "/api/logger/start": "recording",
                                  "/api/logger/stop": "stopped"}[route]})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server

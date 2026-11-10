"""Local telemetry, device controls, and project configuration over HTTP."""

from dataclasses import asdict
from datetime import datetime, timezone
import gzip
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
import json
import os
from pathlib import Path
import sqlite3
from threading import Event, Lock, Thread
from urllib.parse import parse_qs, urlsplit
from uuid import uuid4
from zlib import decompress

from ahead.simulator.floorplan import WALL_TYPES
from ahead.simulator.presets import PRESETS
from ahead.simulator.project import SETTINGS, compile_project, prepare_project, validate_draft
from ahead.simulator.telemetry import DEFAULT_INTERVAL_S, TelemetryArchive


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
        interval_s = int(os.environ.get("AHEAD_ARCHIVE_INTERVAL_S", DEFAULT_INTERVAL_S))
        self.archive = TelemetryArchive(path.with_suffix(".telemetry.sqlite3"), interval_s)
        self.run_id = uuid4().hex
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
            if callback is not None:
                self.run_id = uuid4().hex

    def remote_command(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"device", "field", "value"}:
            raise ValueError("Expected device, field, and value")
        with self.lock:
            if self.remote_callback is None or self.changed.is_set():
                raise ValueError("Device controls are unavailable while the simulator starts")
            self.remote_callback(**payload)

    def record(self, scenario, sample, diagnostic, *, archive=False):
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
                run_id = self.run_id
            else:
                return
        if archive:
            self.archive.record(run_id, sample.end_s, encoded)


def serve_console(console: Console, port: int) -> ThreadingHTTPServer:
    resources = files(__package__)
    allowed_hosts = {
        host.strip().lower()
        for host in os.environ.get(
            "AHEAD_ALLOWED_HOSTS", "localhost,127.0.0.1,::1"
        ).split(",")
        if host.strip()
    }
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
            if host and host.lower() in allowed_hosts:
                return True
            self.send({"error": "Console host is not allowed. Configure AHEAD_ALLOWED_HOSTS."}, 403)
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
                case "/api/archive":
                    try:
                        self.send(console.archive.status())
                    except (OSError, sqlite3.Error):
                        self.send({"error": "Could not read the telemetry archive"}, 503)
                case "/api/archive/export":
                    self.export_archive()
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

        def export_archive(self):
            query = parse_qs(urlsplit(self.path).query)
            try:
                start, end = (int(query[key][0]) for key in ("start_ms", "end_ms"))
                if not 0 <= start <= end <= 253402300799999:
                    raise ValueError
            except (KeyError, ValueError):
                self.send({"error": "Choose a valid start and end time"}, 400)
                return
            if not console.archive.path.is_file():
                self.send({"error": "No telemetry has been archived yet"}, 404)
                return
            started = False
            try:
                with console.archive.range(start, end) as rows:
                    row = rows.fetchone()
                    if row is None:
                        self.send({"error": "No samples in the selected period"}, 404)
                        return
                    self.send_response(200)
                    self.send_header("Content-Type", "application/gzip")
                    self.send_header("Content-Disposition",
                                     'attachment; filename="ahead-telemetry.jsonl.gz"')
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    started = True
                    with gzip.GzipFile(fileobj=self.wfile, mode="wb", compresslevel=1) as out:
                        metadata = {"type": "metadata", "format": "ahead-simulator-jsonl-v2",
                                    "start_ms": start, "end_ms": end,
                                    "current_sample_interval_s": console.archive.interval_s}
                        out.write(json.dumps(metadata).encode() + b"\n")
                        while row is not None:
                            captured_ms, run_id, elapsed_s, state = row
                            timestamp = datetime.fromtimestamp(captured_ms / 1000, timezone.utc)
                            sample = {"type": "sample", "captured_utc": timestamp.isoformat(timespec="milliseconds"),
                                      "run_id": run_id, "elapsed_s": elapsed_s}
                            prefix = json.dumps(sample).encode()[:-1] + b', "state": '
                            out.write(prefix + decompress(state) + b"}\n")
                            row = rows.fetchone()
            except (BrokenPipeError, ConnectionResetError):
                return
            except sqlite3.Error:
                if not started:
                    self.send({"error": "Could not read the telemetry archive"}, 503)
                else:
                    raise

        def do_POST(self):
            if not self.local_request():
                return
            route = urlsplit(self.path).path
            if route not in ("/api/project", "/api/apply", "/api/remote"):
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
                else:
                    console.update(payload, route == "/api/apply")
            except (ValueError, TypeError, KeyError, OverflowError) as error:
                self.send({"error": str(error)}, 400)
                return
            except OSError:
                self.send({"error": "Could not persist the configuration"}, 500)
                return
            self.send({"status": {"/api/project": "saved", "/api/apply": "applying",
                                  "/api/remote": "accepted"}[route]})

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    Thread(target=server.serve_forever, daemon=True).start()
    return server

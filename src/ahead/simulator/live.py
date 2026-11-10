"""Run a wall-clock house simulation with Home Assistant MQTT discovery."""

import argparse
from dataclasses import dataclass, replace
import json
import logging
from math import isfinite
from pathlib import Path
import re
import signal
from threading import Event, Lock
from time import monotonic

import paho.mqtt.client as mqtt

from ahead.control import ControlPlan, Observation
from ahead.simulator.climate import ClimateControls
from ahead.simulator.console import Console, serve_console
from ahead.simulator.project import prepare_project
from ahead.simulator.scenario import Sample, Scenario, load_scenario


LOGGER = logging.getLogger(__name__)
IDENTIFIER = re.compile(r"[a-z0-9_-]+", re.IGNORECASE)


@dataclass(frozen=True)
class Actuator:
    kind: str
    key: str
    modulating: bool
    reversible: bool = False

    @property
    def id(self) -> str:
        return f"{self.kind}_{self.key}"

    @property
    def limits(self) -> tuple[int, int, str]:
        if self.kind == "room_setpoint":
            return 10, 35, "°C"
        if self.kind == "water_setpoint":
            return 20, 80, "°C"
        return (-100 if self.reversible else 0), 100, "%"

    def parse(self, payload: bytes) -> float:
        if not self.modulating:
            return {b"ON": 1.0, b"OFF": 0.0}[payload]
        percent = float(payload)
        lower, upper, unit = self.limits
        if not isfinite(percent) or not lower <= percent <= upper:
            raise ValueError("Value is outside the supported range")
        return percent / 100 if unit == "%" else percent

    def state(self, request: float) -> str:
        if self.modulating:
            return f"{request * 100 if self.limits[2] == '%' else request:.4f}"
        return "ON" if request else "OFF"


class MqttHouse:
    """External controller port and MQTT representation of the simulated house."""

    def __init__(self, scenario: Scenario, house_id: str, discovery_prefix: str,
                 discovery_registry: dict | None = None):
        self.scenario = scenario
        self.house_id = house_id
        self.root = f"ahead/{house_id}"
        self.climate = ClimateControls(scenario.house, self.root)
        self._resume_topic = f"{self.root}/automatic_heat_calls/press"
        self._elapsed_s = 0.0
        self.discovery_registry = discovery_registry if discovery_registry is not None else {}
        self.discovery_topic = f"{discovery_prefix}/device/ahead_{house_id}/config"
        equipment = scenario.house.equipment
        self.actuators = tuple(
            [Actuator("source", name, source.modulating, source.reversible) for name, source in equipment.sources.items()]
            + [Actuator("valve", zone, True) for zone in sorted(equipment.valves)]
            + [
                Actuator(kind, name, True)
                for name, source in scenario.house.sources.items()
                for kind, allowed in (
                    ("room_setpoint", source.room_setpoint_control),
                    ("water_setpoint", source.water_setpoint_control),
                    ("fan", source.fan_control),
                ) if allowed
            ]
        )
        for identifier in (house_id, *scenario.house.zones, *equipment.sources, *equipment.valves):
            if not IDENTIFIER.fullmatch(identifier):
                raise ValueError(f"MQTT identifiers must contain only letters, digits, - or _: {identifier}")
        self._commands = {
            self._topic(actuator.id, "set"): actuator for actuator in self.actuators
        }
        self._requests: dict[Actuator, float] = {}
        self._local_requests: set[Actuator] = set()
        self._hydronic = {
            f"hydronic_{actuator.key}": actuator for actuator in self.actuators
            if self._publish_actuator(actuator) and actuator.kind == "source"
        }
        self._automatic_sources = {
            actuator.key for actuator in self._hydronic.values()
            if scenario.house.sources[actuator.key].automatic_heat_calls
        }
        self._separate_meter = any(
            source.heat_pump_tariff for source in scenario.house.sources.values()
        )
        self._water_targets = {
            actuator.key: actuator for actuator in self.actuators if actuator.kind == "water_setpoint"
        }
        self.remote_devices = self.climate.remote_catalog()
        for key, actuator in self._hydronic.items():
            options = ["auto", "on", "off"] if actuator.key in self._automatic_sources else ["off", "on"]
            fields = {"mode": {"label": "Heat request", "options": options}}
            if actuator.modulating:
                fields["output"] = {"label": "Manual output request", "min": 1, "max": 100, "step": 1, "unit": "%"}
            if actuator.key in self._water_targets:
                fields["water_target"] = {"label": "Water target", "min": 20, "max": 80, "step": 1, "unit": "°C"}
            self.remote_devices[key] = {"group": "sources", "device": actuator.key, "fields": fields}
        self._available: frozenset[str] = frozenset()
        self._lock = Lock()
        self.connected = Event()

    def _topic(self, entity: str, suffix: str) -> str:
        return f"{self.root}/{entity}/{suffix}"

    @property
    def availability_topic(self) -> str:
        return f"{self.root}/availability"

    def _publish_actuator(self, actuator):
        return (actuator.kind in {"source", "water_setpoint"}
                and self.scenario.house.sources[actuator.key].direct_zone is None)

    def remote_command(self, device, field, value):
        if (not isinstance(device, str) or not isinstance(field, str)
                or type(value) not in (str, int, float)):
            raise ValueError("Expected device, setting, and a text or numeric value")
        if field not in self.remote_devices.get(device, {}).get("fields", {}):
            raise ValueError("This device does not support that setting")
        payload = str(value).encode()
        with self._lock:
            unit = self.climate.units.get(device)
            if unit:
                self._apply_climate_command(unit, field, payload)
                return
            actuator = self._hydronic[device]
            if field == "mode":
                if value == "auto" and actuator.key in self._automatic_sources:
                    self._requests.pop(actuator, None)
                    self._local_requests.discard(actuator)
                    return
                if value not in ("on", "off"):
                    raise ValueError("Unsupported heat request mode")
                request = (self._requests.get(actuator) or 1.0) if value == "on" else 0.0
            elif field == "output":
                request = actuator.parse(payload)
                if not request:
                    raise ValueError("Use Off to stop the heat request")
            else:
                actuator = self._water_targets[actuator.key]
                request = actuator.parse(payload)
            self._requests[actuator] = request
            self._local_requests.add(actuator)

    def _apply_climate_command(self, unit, field, payload):
        unit.apply(field, payload, self._elapsed_s)
        if field in {"mode", "target", "preset", "fan", "humidity_target"}:
            kinds = {"valve"} if unit.kind == "trv" else {"source", "room_setpoint", "fan"}
            for actuator in self.actuators:
                if actuator.kind in kinds and actuator.key == unit.name:
                    self._requests.pop(actuator, None)
                    self._local_requests.discard(actuator)

    def discovery(self) -> dict:
        components = {}
        temperatures = {"outdoor": "Outdoor", **{
            f"zone_{zone}": zone.replace("_", " ").title()
            for zone, model in self.scenario.house.zones.items() if model.expose_sensor
        }}
        if self.scenario.house.water_c is not None and not self.scenario.environment:
            temperatures.update({"supply": "Supply water", "return": "Return water"})
            components["flow"] = self._sensor("flow", "Water flow", "L/min")
        for key, label in temperatures.items():
            components[key] = self._sensor(key, f"{label} temperature", "°C", "temperature")
        for key, label in (("electricity", "Electricity used"), ("gas", "Gas energy used")):
            components[key] = self._sensor(key, label, "kWh", "energy", "total_increasing")
        if self._separate_meter:
            components["house_electricity"] = self._sensor(
                "house_electricity", "House electricity used", "kWh", "energy", "total_increasing"
            )
            components["heat_pump_electricity"] = self._sensor(
                "heat_pump_electricity", "Heat-pump tariff electricity used", "kWh", "energy", "total_increasing"
            )
            components["heat_pump_price"] = self._sensor("heat_pump_price", "Heat-pump electricity tariff", None)
        components["elapsed"] = self._sensor("elapsed", "Simulation elapsed time", "s", "duration")
        if self.scenario.environment:
            for name, loop in self.scenario.house.hydronics.loops.items():
                if not loop.expose_sensors:
                    continue
                for key, label, unit, device_class in (
                    ("supply", "supply temperature", "°C", "temperature"),
                    ("return", "return temperature", "°C", "temperature"),
                    ("flow", "water flow", "L/min", None),
                ):
                    component = f"circuit_{name}_{key}"
                    components[component] = self._sensor(component, f"{name} {label}", unit, device_class)
            components["forecast"] = self._sensor("forecast", "Outdoor forecast", "°C", "temperature")
        for key, label in (("electricity_price", "Electricity tariff"), ("gas_price", "Gas tariff")):
            components[key] = self._sensor(key, label, None)
        for actuator in self.actuators:
            if not self._publish_actuator(actuator):
                continue
            kind = "number" if actuator.modulating else "switch"
            key = actuator.id
            label = actuator.key.replace("_", " ").title()
            label += " " + {
                "valve": "valve", "source": "request", "room_setpoint": "room setpoint",
                "water_setpoint": "water target", "fan": "fan request",
            }[actuator.kind]
            component = {
                "platform": kind,
                "name": label,
                "unique_id": f"ahead_{self.house_id}_{key}",
                "state_topic": self._topic(key, "state"),
                "command_topic": self._topic(key, "set"),
                "qos": 1,
                "retain": False,
                "availability": [{"topic": self.availability_topic}],
            }
            if actuator.modulating:
                lower, upper, unit = actuator.limits
                component.update({"min": lower, "max": upper, "step": 1, "unit_of_measurement": unit})
            if actuator.kind == "source":
                component["availability"].append({"topic": self._topic(key, "availability")})
                component["availability_mode"] = "all"
                if self.scenario.house.sources[actuator.key].report_heat:
                    power_key = f"heat_{actuator.key}"
                    components[power_key] = self._sensor(
                        power_key, f"{actuator.key.replace('_', ' ').title()} heat output", "kW", "power"
                    )
            components[key] = component
        identifier = f"ahead_{self.house_id}"
        children = []
        for unit in self.climate.units.values():
            availability = [{"topic": self.availability_topic}]
            if unit.kind == "ac":
                availability.append({"topic": self._topic("source_"+unit.name, "availability")})
            name = unit.name.replace("_", " ").title()
            label = "HVAC"
            if unit.kind == "trv":
                label = "Floor thermostat" if self.scenario.house.hydronics.emitters[unit.name].slab_capacity else "TRV"
            children.append({
                "identifiers": [identifier+"_"+unit.key],
                "name": f"{name} {label}",
                "components": self.climate.components(unit, identifier, availability),
            })
        for actuator in self._hydronic.values():
            name = actuator.key
            device_components = {actuator.id: components.pop(actuator.id)}
            for key in (f"water_setpoint_{name}", f"heat_{name}"):
                if key in components:
                    device_components[key] = components.pop(key)
            source_model = self.scenario.house.sources[name]
            if source_model.pump_kw > 0:
                pump_key = f"pump_{name}"
                device_components[pump_key] = {
                    "platform": "binary_sensor", "name": "Circulation pump",
                    "unique_id": identifier+"_"+pump_key,
                    "state_topic": self._topic(pump_key, "state"),
                    "availability": [{"topic": self.availability_topic}],
                }
            children.append({
                "identifiers": [identifier+"_hydronic_"+name],
                "name": name.replace("_", " ").title(),
                "components": device_components,
            })
        if self._automatic_sources and any(unit.kind == "trv" for unit in self.climate.units.values()):
            components["automatic_heat_calls"] = {
                "platform":"button", "name":"Resume automatic heat calls",
                "unique_id":identifier+"_automatic_heat_calls", "command_topic":self._resume_topic,
                "availability":[{"topic":self.availability_topic}], "qos":1,
            }
        return {
            "device": {
                "identifiers": [f"ahead_{self.house_id}"],
                "name": f"AHEAD {self.house_id}",
                "manufacturer": "AHEAD",
                "model": "Simulated house",
            },
            "origin": {"name": "AHEAD", "support_url": "https://github.com/szokezoltan95/AHEAD"},
            "components": components,
            **({"child_devices": children} if children else {}),
        }

    def _sensor(self, key, name, unit, device_class=None, state_class="measurement") -> dict:
        sensor = {
            "platform": "sensor",
            "name": name,
            "unique_id": f"ahead_{self.house_id}_{key}",
            "state_topic": self._topic(key, "state"),
            "state_class": state_class,
            "availability": [{"topic": self.availability_topic}],
        }
        if unit:
            sensor["unit_of_measurement"] = unit
        if device_class:
            sensor["device_class"] = device_class
        return sensor

    def on_connect(self, client, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            LOGGER.error("MQTT connection rejected: %s", reason_code)
            return
        topics = [*self._commands, *self.climate.commands]
        if self._automatic_sources:
            topics.append(self._resume_topic)
        if topics:
            client.subscribe([(topic, 1) for topic in topics])
        discovery = self.discovery()
        self._discovery_removals(discovery)
        components = discovery["components"]
        components.setdefault("automatic_heat_calls", {"platform": "button"})
        child_components = {name for child in discovery.get("child_devices", ())
                            for name, component in child["components"].items() if "unique_id" in component}
        for actuator in self.actuators:
            if not self._publish_actuator(actuator):
                components.setdefault(actuator.id, {"platform": "number" if actuator.modulating else "switch"})
            if actuator.kind == "source" and f"heat_{actuator.key}" not in child_components:
                components.setdefault(f"heat_{actuator.key}", {"platform": "sensor"})
        client.publish(self.discovery_topic, json.dumps(discovery), qos=1, retain=True)
        self.connected.set()
        LOGGER.info("Connected to MQTT as %s", self.house_id)

    def _discovery_removals(self, discovery):
        registry = self.discovery_registry
        groups = [(None, discovery["components"])]
        for child in discovery.get("child_devices", ()):
            groups.append((child["identifiers"][0], child["components"]))
        active = {key for key, _ in groups}
        active_components = {name for _, components in groups for name in components}
        for key, components in groups:
            previous = registry.setdefault(key, {})
            for name in previous.keys() - active_components:
                components[name] = {"platform":previous[name]}
            previous.update({name:item["platform"] for name,item in components.items()})
        for key in registry.keys() - active:
            removed = {name: {"platform": platform} for name, platform in registry[key].items()
                       if name not in active_components}
            if removed:
                discovery.setdefault("child_devices", []).append({"identifiers": [key], "components": removed})

    def on_disconnect(self, client, userdata, flags, reason_code, properties):
        self.connected.clear()
        with self._lock:
            self._requests = {key: value for key, value in self._requests.items() if key in self._local_requests}

    def on_message(self, client, userdata, message):
        if message.retain:
            return
        if message.topic == self._resume_topic:
            if message.payload == b"PRESS":
                with self._lock:
                    self._requests = {
                        actuator:value for actuator,value in self._requests.items()
                        if not (actuator.kind == "source" and actuator.key in self._automatic_sources)
                    }
                    self._local_requests.intersection_update(self._requests)
            return
        climate = self.climate.commands.get(message.topic)
        if climate:
            unit, field = climate
            try:
                with self._lock:
                    self._apply_climate_command(unit, field, message.payload)
            except (UnicodeDecodeError, ValueError, KeyError):
                LOGGER.warning("Ignored invalid climate command for %s", unit.key)
            return
        actuator = self._commands.get(message.topic)
        if actuator is None:
            return
        try:
            value = actuator.parse(message.payload)
        except (ValueError, KeyError):
            LOGGER.warning("Ignored invalid command for %s", actuator.id)
            return
        with self._lock:
            if actuator.kind == "source" and actuator.key not in self._available:
                return
            self._requests[actuator] = value
            self._local_requests.discard(actuator)
        LOGGER.info("Accepted %s request: %s%s", actuator.id, actuator.state(value), actuator.limits[2]
                    if actuator.modulating else "")

    def decide(self, observation: Observation) -> ControlPlan:
        with self._lock:
            self._available = observation.available_sources
            self._elapsed_s = observation.elapsed_s
            for actuator in self.actuators:
                if (actuator.kind == "source" and actuator.key not in self._available
                        and actuator not in self._local_requests):
                    self._requests.pop(actuator, None)
            requests = {kind: {} for kind in ("source", "valve", "room_setpoint", "fan", "water_setpoint")}
            for actuator, request in self._requests.items():
                requests[actuator.kind][actuator.key] = request
            plan = self.climate.plan(observation, requests)
            for key, actuator in self._hydronic.items():
                name = actuator.key
                manual = requests["source"].get(name)
                mode = "on" if manual else "off"
                if manual is None and name in self._automatic_sources:
                    mode = "auto"
                target = self.scenario.house.hydronics.loops[self.scenario.house.sources[name].loop].target_c
                plan.climate_states[key] = {
                    "kind": "hydronic", "device": name, "mode": mode,
                    "output": 100 * (manual or 1), "water_target": requests["water_setpoint"].get(name, target),
                }
            return plan

    def publish_state(self, client, sample: Sample) -> None:
        readings = {
            **{f"zone_{name}": value for name, value in sample.observation.indoor_c.items()
               if self.scenario.house.zones[name].expose_sensor},
            "outdoor": sample.observation.outdoor_c,
            "electricity": sample.electricity_kwh,
            "gas": sample.gas_kwh,
            "elapsed": sample.end_s,
            "electricity_price": sample.observation.electricity_price_kwh,
            "gas_price": sample.observation.gas_price_kwh,
            **{f"heat_{name}": value for name, value in self.scenario.house.source_heat_kw.items()
               if self.scenario.house.sources[name].report_heat
               and self.scenario.house.sources[name].direct_zone is None},
        }
        if self._separate_meter:
            totals = self.scenario.house.totals
            rate = sample.observation.heat_pump_price_kwh
            readings.update(house_electricity=totals.house_electricity_kwh,
                            heat_pump_electricity=totals.heat_pump_electricity_kwh,
                            heat_pump_price=sample.observation.electricity_price_kwh if rate is None else rate)
        if self.scenario.environment:
            readings["forecast"] = sample.observation.forecast_outdoor_c
            hydronics = self.scenario.house.hydronics
            for name, temperature in hydronics.water.items():
                if not hydronics.loops[name].expose_sensors:
                    continue
                circuit = hydronics.circuits.get(name, {})
                readings.update({
                    f"circuit_{name}_supply": temperature,
                    f"circuit_{name}_return": circuit.get("return_c"),
                    f"circuit_{name}_flow": circuit.get("flow_l_min"),
                })
        if sample.water_c is not None and not self.scenario.environment:
            readings.update({
                "supply": sample.water_c, "return": sample.return_c, "flow": sample.flow_l_min,
            })
        for key, value in readings.items():
            payload = "None" if value is None else f"{value:.5f}"
            client.publish(self._topic(key, "state"), payload, retain=True)
        for name, source in self.scenario.house.sources.items():
            if source.direct_zone is None and source.pump_kw > 0:
                pump_on = bool(self.scenario.house.source_details.get(name, {}).get("pump_on", False))
                client.publish(self._topic(f"pump_{name}", "state"),
                               "ON" if pump_on else "OFF", retain=True)
        for actuator in self.actuators:
            if actuator.kind == "source":
                requested = sample.plan.sources
                status = "online" if actuator.key in sample.observation.available_sources else "offline"
                client.publish(self._topic(actuator.id, "availability"), status, qos=1, retain=True)
            if not self._publish_actuator(actuator):
                continue
            if actuator.kind == "water_setpoint":
                requested = sample.plan.water_setpoints_c
            source = self.scenario.house.sources[actuator.key]
            default = 0 if actuator.kind == "source" else self.scenario.house.hydronics.loops[source.loop].target_c
            value = requested.get(actuator.key, default)
            state = "None" if value is None else actuator.state(value)
            client.publish(self._topic(actuator.id, "state"), state, retain=True)
        self.climate.publish(client, sample)
        client.publish(self.availability_topic, "online", qos=1, retain=True)


def run_live(scenario: Scenario, host: str, port: int, house_id: str,
             discovery_prefix: str, publish_interval: int, stop: Event,
             console: Console | None = None, diagnostic: dict | None = None,
             discovery_registry: dict | None = None) -> None:
    # The research scenario's decision interval is separate from the live clock.
    scenario = replace(scenario, step_s=1)
    bridge = MqttHouse(scenario, house_id, discovery_prefix, discovery_registry)
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=f"ahead-{house_id}")
    client.on_connect = bridge.on_connect
    client.on_disconnect = bridge.on_disconnect
    client.on_message = bridge.on_message
    client.will_set(bridge.availability_topic, "offline", qos=1, retain=True)
    client.connect_async(host, port, keepalive=15)
    client.loop_start()
    try:
        if console:
            console.bind_remote(bridge.remote_devices, bridge.remote_command)
        clock_start = monotonic()
        for sample in scenario.run(bridge, continuous=True):
            if stop.wait(max(0, clock_start + sample.end_s - monotonic())) or (
                console and console.changed.is_set()
            ):
                break
            publish_due = sample.end_s == 1 or sample.end_s % publish_interval == 0
            archive_due = console is not None and (sample.end_s - 1) % console.archive.interval_s == 0
            if console and (publish_due or archive_due):
                console.record(scenario, sample, diagnostic, archive=archive_due)
            if publish_due and bridge.connected.is_set():
                bridge.publish_state(client, sample)
    finally:
        if console:
            console.bind_remote({}, None)
        if client.is_connected():
            message = client.publish(bridge.availability_topic, "offline", qos=1, retain=True)
            try:
                message.wait_for_publish(timeout=2)
            except RuntimeError:
                pass
        client.disconnect()
        client.loop_stop()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--host", default="mqtt")
    parser.add_argument("--port", type=int, default=1883)
    parser.add_argument("--house-id", default="house")
    parser.add_argument("--discovery-prefix", default="homeassistant")
    parser.add_argument("--publish-interval", type=int, default=2, metavar="SECONDS")
    parser.add_argument("--console-port", type=int)
    parser.add_argument("--project-store", type=Path, default=Path("ahead-project.json"))
    args = parser.parse_args()
    if args.publish_interval < 1:
        parser.error("--publish-interval must be positive")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    stop = Event()

    def request_stop(signum, frame):
        stop.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        source_text = args.scenario.read_text(encoding="utf-8-sig")
        default_project = json.loads(source_text) if source_text.lstrip().startswith("{") else None
        console = Console(args.project_store, default_project) if args.console_port else None
        server = serve_console(console, args.console_port) if console else None
        registry = {}
        try:
            while not stop.is_set():
                pending = console.next_run() if console else None
                if pending is None:
                    pending = (prepare_project(default_project) if default_project is not None
                               else (load_scenario(args.scenario), None))
                scenario, diagnostic = pending
                run_live(scenario, args.host, args.port, args.house_id, args.discovery_prefix,
                         args.publish_interval, stop, console, diagnostic, registry)
                if console is None:
                    break
        finally:
            if server:
                server.shutdown()
                server.server_close()
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))


if __name__ == "__main__":
    main()

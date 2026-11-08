from dataclasses import replace
from itertools import islice
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ahead.control import Observation
from ahead.simulator.scenario import load_scenario

try:
    from ahead.simulator.live import MqttHouse
except ModuleNotFoundError as error:
    if error.name != "paho":
        raise
    MqttHouse = None


EXAMPLE = Path(__file__).resolve().parents[1] / "scenarios" / "hybrid_house.toml"


@unittest.skipIf(MqttHouse is None, "Install the live extra to test MQTT")
class LiveTests(unittest.TestCase):
    def setUp(self):
        self.scenario = replace(load_scenario(EXAMPLE), step_s=1, duration_s=1)
        self.bridge = MqttHouse(self.scenario, "test", "homeassistant")
        self.observation = Observation(0, {}, 0, frozenset({"gas_boiler", "living_room_heat_pump"}))
        self.bridge.decide(self.observation)

    def command(self, entity, payload, retain=False):
        self.bridge.on_message(None, None, SimpleNamespace(
            topic=f"ahead/test/{entity}/set", payload=payload, retain=retain,
        ))

    def test_discovery_matches_command_and_state_topics(self):
        discovery = self.bridge.discovery()
        components = discovery["components"]
        devices = {child["identifiers"][0]: child["components"] for child in discovery["child_devices"]}
        self.assertEqual(components["zone_kitchen"]["platform"], "sensor")
        boiler = devices["ahead_test_hydronic_gas_boiler"]
        self.assertEqual(boiler["source_gas_boiler"]["platform"], "switch")
        self.assertNotIn("source_living_room_heat_pump", components)
        self.assertNotIn("valve_kitchen", components)
        self.assertNotIn("fan_living_room_heat_pump", components)
        self.assertNotIn("heat_gas_boiler", boiler)

    def test_commands_drive_continuous_physics_past_batch_duration(self):
        self.command("source_gas_boiler", b"ON")
        self.command("valve_kitchen", b"100")
        samples = list(islice(self.scenario.run(self.bridge, continuous=True), 3))
        self.assertEqual(samples[-1].end_s, 3)
        self.assertGreater(samples[-1].water_c, samples[0].water_c)
        self.assertGreater(samples[-1].gas_kwh, 0)
        client = Mock()
        self.bridge.publish_state(client, samples[-1])
        client.publish.assert_any_call("ahead/test/source_gas_boiler/state", "ON", retain=True)
        client.publish.assert_any_call("ahead/test/availability", "online", qos=1, retain=True)

    def test_retained_and_invalid_commands_are_ignored_and_disconnect_resets(self):
        self.command("source_living_room_heat_pump", b"80", retain=True)
        with self.assertLogs("ahead.simulator.live", level="WARNING"):
            self.command("source_living_room_heat_pump", b"nan")
        self.assertEqual(self.bridge.decide(self.observation).sources, {})
        self.command("source_living_room_heat_pump", b"40")
        self.assertEqual(self.bridge.decide(self.observation).sources, {"living_room_heat_pump": 0.4})
        self.bridge.on_disconnect(None, None, None, None, None)
        self.assertEqual(self.bridge.decide(self.observation).sources, {})

    def test_unavailable_source_clears_request(self):
        self.command("source_gas_boiler", b"ON")
        unavailable = replace(self.observation, available_sources=frozenset())
        self.assertEqual(self.bridge.decide(unavailable).sources, {})
        self.command("source_gas_boiler", b"ON")
        self.assertEqual(self.bridge.decide(self.observation).sources, {})

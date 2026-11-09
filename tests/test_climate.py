from dataclasses import replace
from importlib.resources import files
import json
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from ahead.control import Observation
from ahead.simulator.climate import ClimateControls
from ahead.simulator.house import Conditions
from ahead.simulator.moisture import humidity_ratio
from ahead.simulator.project import compile_project
from ahead.simulator.scenario import load_scenario

try:
    from ahead.simulator.live import MqttHouse
except ModuleNotFoundError as error:
    if error.name != "paho":
        raise
    MqttHouse = None


EXAMPLE = Path(__file__).resolve().parents[1] / "scenarios" / "hybrid_house.toml"
PUMP = "living_room_heat_pump"
AVAILABLE = frozenset({"gas_boiler", PUMP})


class ClimateLogicTests(unittest.TestCase):
    def setUp(self):
        self.house = load_scenario(EXAMPLE).house
        self.controls = ClimateControls(self.house,"ahead/test")
        self.raw = {key:{} for key in ("source","valve","room_setpoint","fan","water_setpoint")}

    def observation(self, seconds, temperature, humidity=55):
        return Observation(seconds, {"living":temperature, "kitchen":temperature}, 3, AVAILABLE,
                           indoor_humidity={"living":humidity})

    def test_external_temperature_offset_and_expiry_control_trv(self):
        unit = self.controls.units["trv_kitchen"]
        for field,value in (("mode",b"heat"),("target",b"23"),("sensor_source",b"external_input"),
                            ("external_temperature",b"24"),("sensor_offset",b"-2"),("overshoot",b".4")):
            unit.apply(field,value,0)
        plan = self.controls.plan(self.observation(1,24),self.raw)
        self.assertGreater(plan.valves["kitchen"],0)
        self.assertEqual(plan.sources["gas_boiler"],1)
        self.assertEqual(plan.climate_states[unit.key]["control_c"],22)
        self.assertEqual(plan.climate_states[unit.key]["effective_target_c"],22.6)
        expired = self.controls.plan(self.observation(901,24),self.raw)
        self.assertEqual(expired.climate_states[unit.key]["sensor_source"],"built_in")
        self.assertIsNone(expired.climate_states[unit.key]["external_c"])

    def test_adaptive_trv_learns_coasting_above_target(self):
        unit = self.controls.units["trv_kitchen"]
        for field,value in (("mode",b"heat"),("preset",b"adaptive"),("target",b"21"),
                            ("sensor_source",b"room_sensor")):
            unit.apply(field,value,0)
        for seconds,temperature in ((1,18),(60,21),(160,22),(660,21.2)):
            plan = self.controls.plan(self.observation(seconds,temperature),self.raw)
        self.assertAlmostEqual(plan.climate_states[unit.key]["learned_overshoot_c"],.3)
        next_cycle = self.controls.plan(self.observation(700,18),self.raw)
        self.assertAlmostEqual(next_cycle.climate_states[unit.key]["effective_target_c"],20.7)

    def test_ac_modulates_and_auto_can_choose_cooling(self):
        unit = self.controls.units["ac_"+PUMP]
        unit.apply("mode",b"auto",0)
        unit.apply("target",b"22",0)
        far = self.controls.plan(self.observation(1,17),self.raw)
        near = self.controls.plan(self.observation(2,21.5),self.raw)
        cooling = self.controls.plan(self.observation(3,25),self.raw)
        self.assertGreater(far.sources[PUMP], near.sources[PUMP])
        self.assertGreater(near.sources[PUMP],0)
        self.assertLess(cooling.sources[PUMP],0)

    def test_fan_only_consumes_power_without_running_compressor(self):
        unit = self.controls.units["ac_"+PUMP]
        unit.apply("mode",b"fan_only",0)
        unit.apply("fan",b"high",0)
        plan = self.controls.plan(self.observation(1,25),self.raw)
        self.house.advance(1, Conditions(0), plan, AVAILABLE)
        self.assertEqual(self.house.source_heat_kw[PUMP],0)
        self.assertGreater(self.house.source_details[PUMP]["fan_kw"],0)
        self.assertGreater(self.house.totals.electricity_kwh,0)

    def test_dry_mode_removes_moisture_and_uses_latent_capacity(self):
        unit = self.controls.units["ac_"+PUMP]
        unit.apply("mode",b"dry",0)
        unit.apply("target",b"20",0)
        checkpoint = self.house.checkpoint()
        checkpoint["air_c"]["living"] = 26
        checkpoint["moisture"]["living"] = humidity_ratio(26,80)
        self.house.restore_checkpoint(checkpoint)
        plan = self.controls.plan(self.observation(1,26,80),self.raw)
        self.house.advance(30,Conditions(26,outdoor_humidity_pct=80),plan,AVAILABLE)
        self.assertGreater(self.house.source_details[PUMP]["latent_cooling_kw"],0)
        self.assertLess(self.house.checkpoint()["moisture"]["living"],checkpoint["moisture"]["living"])


@unittest.skipIf(MqttHouse is None, "Install the live extra to test MQTT")
class ClimateMqttTests(unittest.TestCase):
    def test_devices_accept_commands_and_publish_actual_actions(self):
        scenario = replace(load_scenario(EXAMPLE),step_s=1)
        bridge = MqttHouse(scenario,"test","homeassistant")
        children = {child["identifiers"][0]:child for child in bridge.discovery()["child_devices"]}
        trv = children["ahead_test_trv_kitchen"]["components"]["trv_kitchen"]
        ac = children["ahead_test_ac_"+PUMP]["components"]["ac_"+PUMP]
        self.assertEqual(trv["modes"],["off","heat"])
        self.assertEqual(set(ac["modes"]),{"off","heat","cool","auto","fan_only","dry"})

        def command(topic,payload):
            bridge.on_message(None,None,SimpleNamespace(topic=topic,payload=payload,retain=False))

        samples = scenario.run(bridge,continuous=True)
        next(samples)
        command(trv["mode_command_topic"],b"heat")
        command(trv["temperature_command_topic"],b"26")
        command(ac["mode_command_topic"],b"fan_only")
        sample = next(samples)
        client = Mock()
        bridge.publish_state(client,sample)
        self.assertEqual(sample.plan.sources["gas_boiler"],1)
        self.assertGreater(sample.plan.valves["kitchen"],0)
        client.publish.assert_any_call(ac["action_topic"],"fan",retain=True)
        client.publish.assert_any_call(trv["temperature_state_topic"],"26.000",retain=True)
        command("ahead/test/source_gas_boiler/set",b"OFF")
        self.assertEqual(next(samples).plan.sources["gas_boiler"],0)
        command(bridge.discovery()["components"]["automatic_heat_calls"]["command_topic"],b"PRESS")
        self.assertEqual(next(samples).plan.sources["gas_boiler"],1)

    def test_local_remote_shares_settings_without_exporting_private_diagnostics(self):
        scenario = replace(load_scenario(EXAMPLE), step_s=1)
        bridge = MqttHouse(scenario, "test", "homeassistant")
        samples = scenario.run(bridge, continuous=True)
        next(samples)
        bridge.remote_command("trv_kitchen", "mode", "heat")
        bridge.remote_command("trv_kitchen", "target", 26)
        bridge.remote_command("trv_kitchen", "overshoot", 1)
        bridge.remote_command("hydronic_gas_boiler", "mode", "off")
        self.assertEqual(next(samples).plan.sources["gas_boiler"], 0)
        bridge.remote_command("hydronic_gas_boiler", "mode", "auto")
        bridge.remote_command("ac_" + PUMP, "mode", "fan_only")
        with self.assertRaises(ValueError):
            bridge.remote_command("ac_" + PUMP, "humidity_target", 40)
        with self.assertRaises(ValueError):
            bridge.remote_command("trv_kitchen", "target", float("nan"))
        sample = next(samples)
        self.assertEqual(sample.plan.sources["gas_boiler"], 1)
        self.assertEqual(sample.plan.climate_states["trv_kitchen"]["effective_target_c"], 25)
        client = Mock()
        bridge.publish_state(client, sample)
        client.publish.assert_any_call("ahead/test/climate/trv_kitchen/target/state", "26.000", retain=True)
        client.publish.assert_any_call(f"ahead/test/climate/ac_{PUMP}/action/state", "fan", retain=True)
        topics = [call.args[0] for call in client.publish.call_args_list]
        self.assertFalse(any("overshoot" in topic or "/opening/" in topic for topic in topics))

    def test_manual_boiler_and_fixed_open_radiator(self):
        project = json.loads(files("ahead.simulator").joinpath("example.json").read_text())
        project["emitters"][0]["fixed_open"] = True
        project["sources"][0].update(automatic_heat_calls=False, capacity_kw=24,
                                     water_hysteresis_c=6)
        project["sources"][1]["automatic_heat_calls"] = False
        scenario, _ = compile_project(project)
        scenario = replace(scenario, step_s=1)
        bridge = MqttHouse(scenario, "test", "homeassistant")
        self.assertNotIn("trv_living", bridge.remote_devices)
        self.assertNotIn("automatic_heat_calls", bridge.discovery()["components"])
        self.assertEqual(bridge.remote_devices["hydronic_gas_boiler"]["fields"]["mode"]["options"],
                         ["off", "on"])
        with self.assertRaises(ValueError):
            bridge.remote_command("hydronic_gas_boiler", "mode", "auto")
        bridge.remote_command("trv_kitchen", "mode", "heat")
        bridge.remote_command("trv_kitchen", "target", 26)
        samples = scenario.run(bridge, continuous=True)
        next(samples)
        self.assertEqual(scenario.house.source_heat_kw["gas_boiler"], 0)
        self.assertEqual(scenario.house.hydronics.details["living"]["opening"], 1)
        bridge.remote_command("hydronic_gas_boiler", "mode", "on")
        next(samples)
        self.assertEqual(scenario.house.source_heat_kw["gas_boiler"], 24)
        bridge.remote_command("hydronic_gas_boiler", "mode", "off")
        next(samples)
        self.assertEqual(scenario.house.source_heat_kw["gas_boiler"], 0)

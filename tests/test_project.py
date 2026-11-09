from datetime import datetime, timezone
from importlib.resources import files
from itertools import islice
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from urllib.request import Request, urlopen

from ahead.control import ControlPlan
from ahead.simulator.console import Console, serve_console
from ahead.simulator.house import Conditions
from ahead.simulator.project import compile_project, prepare_project
from ahead.simulator.solar import incident_kw_m2, sun_vector


def example():
    return json.loads(files("ahead.simulator").joinpath("example.json").read_text())


class ProjectTests(unittest.TestCase):
    def test_warmup_checkpoint_is_reproducible(self):
        project = example()
        prepared, _ = prepare_project(project)
        snapshot = prepared.house.checkpoint()
        self.assertEqual(prepared.house.elapsed_s, 0)
        self.assertEqual(prepared.house.totals.electricity_kwh, 0)
        self.assertNotIn("attic", prepared.setpoints_c)
        restored, _ = compile_project(project)
        restored.house.restore_checkpoint(snapshot)
        self.assertEqual(snapshot, restored.house.checkpoint())
        self.assertEqual(snapshot, prepare_project(project)[0].house.checkpoint())

    def test_multisplit_shares_capacity_and_rejects_opposing_mode(self):
        project = example()
        project["outdoor_units"][0]["capacity_kw"] = 3
        scenario, _ = compile_project(project)
        house = scenario.house
        names = {"living_room_heat_pump", "bedroom_indoor"}
        available = frozenset(names)
        house.advance(1, Conditions(7), ControlPlan(dict.fromkeys(names, 1), {}), available)
        self.assertAlmostEqual(sum(house.source_heat_kw.values()), 3.0)
        self.assertAlmostEqual(house.source_heat_kw["bedroom_indoor"], 1.5)
        house.advance(1, Conditions(7), ControlPlan(
            {"living_room_heat_pump": -1, "bedroom_indoor": 0.5}, {}
        ), available)
        self.assertLess(house.source_heat_kw["living_room_heat_pump"], 0)
        self.assertEqual(house.source_heat_kw["bedroom_indoor"], 0)

    def test_heat_pump_meter_accounts_for_defrost_and_isolated_pv(self):
        project = example()
        for source in project["sources"]:
            if source["preset"] == "indoor_split":
                source["heat_pump_tariff"] = True
        project["outdoor_units"][0].update(defrost_period_s=10, defrost_duration_s=3)
        scenario, _ = compile_project(project)
        house = scenario.house
        house.elapsed_s = 8
        house.advance(1, Conditions(0, appliance_kw=.4, solar_pv_kw=1,
                                    electricity_price_kwh=.3, heat_pump_price_kwh=.12),
                      ControlPlan({"living_room_heat_pump": 1}, {}),
                      frozenset({"living_room_heat_pump"}))
        totals = house.totals
        self.assertAlmostEqual(totals.house_electricity_kwh, .4 / 3600)
        self.assertAlmostEqual(totals.heat_pump_electricity_kwh, .8 / 3600)
        self.assertAlmostEqual(totals.import_kwh, .8 / 3600)
        self.assertAlmostEqual(totals.export_kwh, .6 / 3600)
        self.assertAlmostEqual(totals.cost, .8 * .12 / 3600)

    def test_underfloor_valve_moves_gradually_and_heats_slab(self):
        scenario, _ = compile_project(example())
        house = scenario.house
        start = house.hydronics.slab["kitchen_floor"]
        house.advance(60, Conditions(0), ControlPlan(
            {"water_heat_pump": 1}, {"kitchen_floor": 1},
        ), frozenset({"water_heat_pump"}))
        emitter = house.diagnostics()["emitters"]["kitchen_floor"]
        self.assertAlmostEqual(emitter["opening"], 0.5)
        self.assertGreater(house.hydronics.slab["kitchen_floor"], start)
        self.assertGreater(emitter["flow_l_min"], 0)

    def test_solar_orientation_and_pv_balance(self):
        moment = datetime(2026, 3, 21, 9, tzinfo=timezone.utc)
        sun = sun_vector(moment, 48, 0)
        east = incident_kw_m2(sun, 90, 90, 0.6, 0.1, 0)
        west = incident_kw_m2(sun, 270, 90, 0.6, 0.1, 0)
        self.assertGreater(east, west)
        scenario, _ = compile_project(example())
        house = scenario.house
        conditions = scenario.environment(12 * 3600, Conditions(4, appliance_kw=0.2))
        house.advance(60, conditions, ControlPlan({}, {}), frozenset())
        totals = house.totals
        self.assertGreater(totals.pv_kwh, 0)
        self.assertAlmostEqual(totals.pv_kwh + totals.import_kwh,
                               totals.export_kwh + totals.electricity_kwh)
        self.assertGreater(house.diagnostics()["balance"]["solar_collector_kw"]["water_collector"], 0)

    def test_dhw_priority_and_draw(self):
        project = example()
        project["sources"][0]["dhw_circuit"] = "hot_water"
        scenario, _ = compile_project(project)
        house = scenario.house
        tank_start = house.hydronics.water["hot_water"]
        house.advance(60, Conditions(5, dhw_draw_l_min={"hot_water": 7}),
                      ControlPlan({}, {}), frozenset())
        self.assertLess(house.hydronics.water["hot_water"], tank_start)
        house.advance(60, Conditions(5), ControlPlan({"gas_boiler": 1}, {}),
                      frozenset({"gas_boiler"}))
        self.assertTrue(house.diagnostics()["sources"]["gas_boiler"]["dhw_priority_active"])


class ConsoleTests(unittest.TestCase):
    def test_draft_apply_persistence_and_readable_live_values(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / "project.json"
            console = Console(path)
            server = serve_console(console, 0)
            root = f"http://127.0.0.1:{server.server_port}"
            try:
                with urlopen(root) as response:
                    self.assertIn(b"House simulator", response.read())
                project = example()
                with urlopen(Request(root + "/api/apply", json.dumps(project).encode(),
                                     {"Content-Type": "application/json"})) as response:
                    self.assertEqual(json.load(response)["status"], "applying")
                scenario, details = console.next_run()
                controller = type("Off", (), {"decide": lambda self, obs: ControlPlan({}, {})})()
                sample = next(islice(scenario.run(controller), 1))
                console.record(scenario, sample, details)
                with urlopen(root + "/api/live") as response:
                    state = json.load(response)
                self.assertIn("fabric_c", state["rooms"]["living"])
                self.assertIn("inside_node_c", state["surfaces"]["south_roof"])
                self.assertIn("slab_c", state["emitters"]["kitchen_floor"])
                restored = Console(path)
                self.assertEqual(restored.next_run()[0].house.checkpoint(), console.initial_state)
            finally:
                server.shutdown()
                server.server_close()

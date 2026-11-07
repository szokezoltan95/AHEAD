import csv
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from ahead.control import ControlPlan, HysteresisController
from ahead.simulator.house import Conditions, House, Partition, Source, WaterLoop, Zone
from ahead.simulator.scenario import load_scenario


ROOT = Path(__file__).resolve().parents[1]
EXAMPLE = ROOT / "scenarios" / "hybrid_house.toml"


def zone(name, temperature, **options):
    return Zone(name, 0.1, 1.0, 0, 0, 0, temperature, 20, **options)


class PhysicsTests(unittest.TestCase):
    def test_partition_conserves_energy_and_equalises_room_temperatures(self):
        house = House(
            (zone("warm", 25), zone("cold", 15)),
            (Partition("warm", "cold", 0.2),),
            (),
        )
        house.advance(3600, Conditions(0), ControlPlan({}, {}), frozenset())
        temperatures = house.indoor_c
        self.assertAlmostEqual(sum(temperatures.values()), 40)
        self.assertGreater(temperatures["warm"], temperatures["cold"])
        self.assertLess(temperatures["warm"] - temperatures["cold"], 0.2)

    def test_radiators_limit_flow_and_conserve_water_and_room_energy(self):
        house = House(
            (
                zone("one", 20, radiator_kw_k=0.15, radiator_flow_l_min=10),
                zone("two", 20, radiator_kw_k=0.15, radiator_flow_l_min=10),
            ),
            (),
            (),
            WaterLoop(0.1, 50, 50, max_flow_l_min=12),
        )
        house.advance(180, Conditions(0), ControlPlan({}, {"one": 1, "two": 1}), frozenset())
        self.assertEqual(house.flow_l_min, 12)
        self.assertLess(house.return_c, house.water_c)
        self.assertGreater(house.indoor_c["one"], 20)
        self.assertAlmostEqual(house.indoor_c["one"], house.indoor_c["two"])
        self.assertAlmostEqual(0.1 * (sum(house.indoor_c.values()) + house.water_c), 9)

    def test_electric_input_cost_and_minimum_cycle_duration(self):
        house = House(
            (zone("room", 20),),
            (),
            (Source("pump", "electricity", 2, 2, direct_zone="room", min_on_s=120,
                    min_off_s=180),),
        )
        conditions = Conditions(0, electricity_price_kwh=0.3)
        available = frozenset({"pump"})
        house.advance(60, conditions, ControlPlan({"pump": 1}, {}), available)
        house.advance(120, conditions, ControlPlan({}, {}), available)
        house.advance(60, conditions, ControlPlan({"pump": 1}, {}), available)
        self.assertEqual(house.source_starts, {"pump": 1})
        self.assertAlmostEqual(house.totals.electricity_kwh, 120 / 3600)
        self.assertAlmostEqual(house.totals.cost, 0.01)

    def test_invalid_command_does_not_advance_house(self):
        house = House((zone("room", 20),), (), ())
        with self.assertRaises(ValueError):
            house.advance(60, Conditions(0), ControlPlan({"unknown": 1}, {}), frozenset())
        self.assertEqual(house.elapsed_s, 0)


class ScenarioTests(unittest.TestCase):
    def test_example_is_repeatable_and_faults_affect_observations_only(self):
        def run():
            scenario = load_scenario(EXAMPLE)
            baseline = HysteresisController(scenario.house.equipment, scenario.setpoints_c, 0.5)
            return list(scenario.run(baseline))

        first = run()
        self.assertEqual(first, run())
        self.assertEqual(len(first), 4320)
        dropout = first[122400 // 60]
        self.assertIsNone(dropout.observation.indoor_c["study"])
        self.assertIsInstance(dropout.actual_indoor_c["study"], float)
        outage = first[165600 // 60]
        self.assertNotIn("living_room_heat_pump", outage.observation.available_sources)
        self.assertEqual(outage.plan.sources["living_room_heat_pump"], 0)
        bias = first[198000 // 60]
        self.assertAlmostEqual(bias.observation.outdoor_c - bias.actual_outdoor_c, 2, delta=0.3)
        self.assertGreater(first[-1].gas_kwh, 0)
        self.assertGreater(first[-1].electricity_kwh, 0)

    def test_cli_exports_aligned_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "trace.csv"
            result = subprocess.run(
                [sys.executable, "-m", "ahead.simulator", str(EXAMPLE), "--csv", str(target)],
                capture_output=True, text=True, check=True,
            )
            self.assertIn('"source_starts"', result.stdout)
            with target.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream))
            self.assertEqual(len(rows), 4320)
            self.assertEqual(rows[0]["start_s"], "0")
            self.assertEqual(rows[-1]["end_s"], "259200")
            self.assertTrue(all(None not in row and None not in row.values() for row in rows))


if __name__ == "__main__":
    unittest.main()

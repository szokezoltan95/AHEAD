from copy import deepcopy
from importlib.resources import files
import json
import math
import unittest

from ahead.control import ControlPlan
from ahead.simulator.house import Conditions
from ahead.simulator.project import compile_project


def example():
    return json.loads(files("ahead.simulator").joinpath("example.json").read_text())


class FloorplanTests(unittest.TestCase):
    def test_adjacent_rooms_share_one_wall_and_window_uses_drawn_area(self):
        project = example()
        for name in ("living", "kitchen"):
            project["floorplan"]["rooms"][name]["derive"] = True
        project["floorplan"]["rooms"]["living"]["walls"] = {
            "west": {"construction": "insulated_block", "openings": [
                {"preset": "triple_glazed", "distance": 2, "width": 1, "height": 1},
            ]}
        }
        _, diagnostic = compile_project(project)
        surfaces = diagnostic["surfaces"]
        walls = [row for name, row in surfaces.items() if name.startswith("plan_") and
                 {row["room"], row["adjacent_room"]} == {"living", "kitchen"}]
        self.assertEqual(len(walls), 1)
        self.assertAlmostEqual(walls[0]["area_m2"], 4 * 2.7)
        west = surfaces["plan_living_west_1"]
        self.assertAlmostEqual(west["area_m2"] - west["opaque_m2"], 1)
        self.assertEqual(west["azimuth_deg"], 270)

    def test_drawn_pipe_pair_carries_flow_and_uses_riser_height(self):
        scenario, diagnostic = compile_project(example())
        house = scenario.house
        self.assertEqual(diagnostic["routed_emitters"], ["bedroom", "kitchen", "living", "study"])
        house.advance(1, Conditions(5), ControlPlan(
            {"gas_boiler": 1}, {"living": 1, "bedroom": 1}
        ), frozenset({"gas_boiler"}))
        pipes = house.diagnostics()["pipes"]
        self.assertAlmostEqual(pipes["bedroom_riser_supply"]["length_m"], 2.7)
        self.assertAlmostEqual(pipes["boiler_run_supply"]["flow_l_min"],
                               2 * pipes["bedroom_riser_supply"]["flow_l_min"])
        self.assertGreater(pipes["living_radiator_run_supply"]["heat_loss_kw"], 0)
        self.assertGreater(pipes["bedroom_riser_return"]["flow_l_min"], 0)
        self.assertTrue(math.isfinite(house.return_c))

    def test_wall_depth_and_endpoint_height_change_geometry(self):
        project = example()
        plan = project["floorplan"]
        plan["nodes"]["kitchen_radiator"]["height_mm"] = 1150
        plan["pipes"]["kitchen_radiator_run_supply"].update(mounting="in_wall", offset_mm=70)
        scenario, _ = compile_project(project)
        pipe = scenario.house.hydronics.pipe_network.pipes["kitchen_radiator_run_supply"]
        self.assertAlmostEqual(pipe.points[1][0], 5 - .07)
        self.assertAlmostEqual(pipe.points[1][2], 1.15)
        self.assertAlmostEqual(pipe.length_m, math.hypot(2.5, 1))

    def test_crossing_or_unfinished_routes_are_not_connected(self):
        project = example()
        plan = project["floorplan"]
        for name in ("kitchen_radiator_run_return",):
            pipe = plan["pipes"][name]
            plan["nodes"]["unconnected"] = deepcopy(plan["nodes"][pipe["first"]])
            pipe["first"] = "unconnected"
        _, diagnostic = compile_project(project)
        self.assertNotIn("kitchen", diagnostic["routed_emitters"])

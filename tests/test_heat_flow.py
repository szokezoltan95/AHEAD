import unittest

from ahead.control import ControlPlan
from ahead.simulator.house import Conditions, House, Partition, Zone
from ahead.simulator.hydronics import Emitter, WaterLoop


class HeatFlowTests(unittest.TestCase):
    def test_radiator_plume_mixes_heat_to_the_far_upper_room_cells(self):
        room = Zone("room", 4 * 3 * 2.7 * 1.2 * 1.005 / 3600,
                    1.5, 0.08, 0.03, 0.002, 20, 25)
        emitter = Emitter(
            "room", "main", 0.001, 5, fixed_open=True, width_m=1.5, height_m=0.6,
            rated_output_kw=1.7, rated_delta_k=50, water_delta_k=10,
            output_exponent=1.3, radiant_fraction=0.25,
            radiator_water_l=6, radiator_metal_kg=30, radiator_metal_cp_j_kgk=500,
        )
        house = House(
            (room,), (), (), loops={"main": WaterLoop(100, 60, 60, max_flow_l_min=20)},
            emitters={"radiator": emitter},
            air_source_positions={"radiator": (0.5, 0.98, 0.18)},
            room_dimensions_m={"room": (4, 3, 2.7)},
        )
        for _ in range(30):
            house.advance(60, Conditions(0), ControlPlan({}, {}), frozenset())

        cells = house.diagnostics()["rooms"]["room"]["air_cells_c"]
        temperatures = [value for layer in cells for row in layer for value in row]
        vectors = [vector for layer in house.diagnostics()["rooms"]["room"]["air_currents"]["vectors_m_s"]
                   for row in layer for vector in row]
        self.assertLess(max(temperatures) - min(temperatures), 3.2)
        self.assertGreater(cells[-1][0][0], 25.5)
        self.assertGreater(max(vector[0] for vector in vectors), 0.05)
        self.assertLess(min(vector[0] for vector in vectors), -0.05)

    def test_links_report_signed_power_and_conserve_energy(self):
        zones = (
            Zone("warm", 1.0, 2.0, 0, 0, 0, 25, 20),
            Zone("cold", 1.0, 2.0, 0, 0, 0, 15, 20),
        )
        house = House(zones, (Partition("cold", "warm", 0.1),), ())
        house.advance(1, Conditions(
            20, door_exchange_kw_k={("warm", "cold"): 0.2},
            radiant_opening_m2={("warm", "cold"): 2},
        ), ControlPlan({}, {}), frozenset())
        state = house.diagnostics()
        links = {link["kind"]: link for link in state["heat_links"]}
        self.assertAlmostEqual(links["conduction"]["kw"], -1, places=3)
        self.assertAlmostEqual(links["airflow"]["kw"], 2, places=3)
        self.assertGreater(links["radiation"]["kw"], 0)
        rooms = state["rooms"]
        self.assertAlmostEqual(sum(room["air_c"] for room in rooms.values()), 40)
        self.assertAlmostEqual(sum(room["fabric_c"] for room in rooms.values()), 40)
        self.assertGreater(rooms["cold"]["fabric_c"], 15)
        house.advance(1, Conditions(20), ControlPlan({}, {}), frozenset())
        self.assertEqual([link["kind"] for link in house.diagnostics()["heat_links"]], ["conduction"])

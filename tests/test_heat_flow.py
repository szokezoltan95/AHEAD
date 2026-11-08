import unittest

from ahead.control import ControlPlan
from ahead.simulator.house import Conditions, House, Partition, Zone


class HeatFlowTests(unittest.TestCase):
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
        self.assertAlmostEqual(links["conduction"]["kw"], -1)
        self.assertAlmostEqual(links["airflow"]["kw"], 2)
        self.assertGreater(links["radiation"]["kw"], 0)
        rooms = state["rooms"]
        self.assertAlmostEqual(sum(room["air_c"] for room in rooms.values()), 40)
        self.assertAlmostEqual(sum(room["fabric_c"] for room in rooms.values()), 40)
        self.assertGreater(rooms["cold"]["fabric_c"], 15)
        house.advance(1, Conditions(20), ControlPlan({}, {}), frozenset())
        self.assertEqual([link["kind"] for link in house.diagnostics()["heat_links"]], ["conduction"])

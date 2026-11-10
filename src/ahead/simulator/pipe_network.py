"""Finite-volume water and steel thermal nodes for every drawn pipe segment."""

from collections import defaultdict
from math import isfinite


WATER_KWH_PER_L_K = 4.186 / 3600
METAL_CP = {"steel": 500.0, "copper": 385.0, "pex": 2300.0, "ppr": 1900.0}


class PipeNetwork:
    def __init__(self, pipes, paths, initial_c=20.0):
        self.pipes = pipes
        self.paths = paths
        self.details = {}
        self.water_c = {name: initial_c for name in pipes}
        self.metal_c = {name: initial_c for name in pipes}

    def checkpoint(self):
        return {"water_c": self.water_c.copy(), "metal_c": self.metal_c.copy()}

    def rates(self):
        for name, pipe in self.pipes.items():
            water_capacity = max(pipe.water_volume_l * WATER_KWH_PER_L_K, 1e-9)
            metal_capacity = max(pipe.metal_mass_kg * METAL_CP[pipe.material] / 3_600_000, 1e-9)
            inner_diameter = max(0.001, (pipe.diameter_mm - 2 * pipe.wall_thickness_mm) / 1000)
            inside_ua = 3.141592653589793 * inner_diameter * pipe.length_m * 0.8
            yield max((inside_ua + pipe.kw_k) / water_capacity,
                      (inside_ua + pipe.kw_k) / metal_capacity)

    def restore_checkpoint(self, snapshot):
        for key, target in (("water_c", self.water_c), ("metal_c", self.metal_c)):
            values = snapshot.get(key)
            if values is None:
                target.update({name: next(iter(self.water_c.values()), 20.0) for name in target})
            elif values.keys() != target.keys() or not all(map(isfinite, values.values())):
                raise ValueError("Pipe thermal checkpoint does not match this house")
            else:
                target.update(values)

    def frame(self, circuit, flows, air, fabric, outdoor, ground, dt_s):
        paths = {name: self.paths[name] for name in flows if name in self.paths}
        pipes = {name: pipe for name, pipe in self.pipes.items() if pipe.circuit == circuit}
        return PipeFrame(self, pipes, paths, flows, air, fabric, outdoor, ground, dt_s)


class PipeFrame:
    def __init__(self, network, pipes, paths, flows, air, fabric, outdoor, ground, dt_s):
        self.network = network
        self.pipes = pipes
        self.paths = paths
        self.flows = flows
        self.dt_h = max(0.0, dt_s / 3600)
        self.ambient = {
            name: (fabric.get(pipe.room, ground) if pipe.mounting in ("in_wall", "underfloor")
                   else air.get(pipe.room, outdoor))
            for name, pipe in pipes.items()
        }
        self.details = {
            name: {
                "circuit": pipe.circuit, "run": pipe.run, "mounting": pipe.mounting, "wall": pipe.wall,
                "room": pipe.room, "length_m": pipe.length_m, "flow_l_min": 0.0,
                "inlet_c": None, "outlet_c": self.network.water_c[name],
                "water_c": self.network.water_c[name], "metal_c": self.network.metal_c[name],
                "water_volume_l": pipe.water_volume_l, "metal_mass_kg": pipe.metal_mass_kg,
                "stored_heat_kwh": 0.0, "heat_loss_kw": 0.0, "ambient_c": self.ambient[name],
                "diameter_mm": pipe.diameter_mm, "wall_thickness_mm": pipe.wall_thickness_mm,
                "material": pipe.material, "insulation_mm": pipe.insulation_mm,
                "offset_mm": pipe.offset_mm, "points": pipe.points,
            } for name, pipe in pipes.items()
        }
        self.air_heat = defaultdict(float)
        self.fabric_heat = defaultdict(float)
        self.loss_kw = 0.0
        self.water_exchange_kw = 0.0
        self.updated = set()

    def _advance_segment(self, pipe_id, flow, inlet, water_rate, first=None, last=None):
        pipe = self.pipes[pipe_id]
        ambient = self.ambient[pipe_id]
        old_water = self.network.water_c[pipe_id]
        old_metal = self.network.metal_c[pipe_id]
        if self.dt_h <= 0:
            # House.step performs a zero-duration exchange after advancing the
            # model to refresh diagnostics. Preserve the current temperatures,
            # but still report the requested live flow and route endpoints.
            self.details[pipe_id].update(
                flow_l_min=flow, inlet_c=inlet if flow else None,
                outlet_c=old_water, water_c=old_water, metal_c=old_metal,
                flow_from=first, flow_to=last,
            )
            if flow > 0:
                self.updated.add(pipe_id)
            return old_water
        water_capacity = max(pipe.water_volume_l * WATER_KWH_PER_L_K, 1e-9)
        metal_capacity = max(pipe.metal_mass_kg * METAL_CP[pipe.material] / 3_600_000, 1e-9)
        advective_rate = max(0.0, flow * water_rate)
        inside_diameter_m = max(0.001, (pipe.diameter_mm - 2 * pipe.wall_thickness_mm) / 1000)
        inside_area_m2 = 3.141592653589793 * inside_diameter_m * pipe.length_m
        inside_ua = max(0.00001, inside_area_m2 * 0.8)  # turbulent water-side film, kW/K
        outside_ua = pipe.kw_k
        aw = water_capacity / self.dt_h + advective_rate + inside_ua
        am = metal_capacity / self.dt_h + inside_ua + outside_ua
        rw = water_capacity / self.dt_h * old_water + advective_rate * inlet
        rm = metal_capacity / self.dt_h * old_metal + outside_ua * ambient
        determinant = aw * am - inside_ua**2
        water_new = (rw * am + inside_ua * rm) / determinant
        metal_new = (rm * aw + inside_ua * rw) / determinant
        outlet = water_new if flow > 0 else old_water
        water_exchange = flow * water_rate * (inlet - outlet) if flow > 0 else 0.0
        heat_loss = outside_ua * (metal_new - ambient)
        self.network.water_c[pipe_id] = water_new
        self.network.metal_c[pipe_id] = metal_new
        self.loss_kw += heat_loss
        self.water_exchange_kw += water_exchange
        if pipe.room:
            target = self.fabric_heat if pipe.mounting in ("in_wall", "underfloor") else self.air_heat
            target[pipe.room] += heat_loss
        self.details[pipe_id].update(
            flow_l_min=flow, inlet_c=inlet if flow else None, outlet_c=outlet,
            water_c=water_new, metal_c=metal_new, heat_loss_kw=heat_loss,
            water_to_segment_kw=water_exchange,
            stored_heat_kwh=(water_capacity * (water_new - ambient) +
                             metal_capacity * (metal_new - ambient)),
            flow_from=first, flow_to=last,
        )
        self.updated.add(pipe_id)
        return outlet

    def supply(self, temperature, water_rate):
        at_node = {}
        demand = defaultdict(float)
        steps = {}
        for emitter, route in self.paths.items():
            for depth, (pipe, first, last) in enumerate(route["supply"]):
                demand[pipe] += self.flows[emitter]
                steps[pipe] = depth, first, last
        for pipe, (_, first, last) in sorted(steps.items(), key=lambda item: item[1][0]):
            if demand[pipe]:
                at_node[last] = self._advance_segment(
                    pipe, demand[pipe], at_node.get(first, temperature), water_rate, first, last
                )
        return {
            name: at_node.get(route["supply"][-1][2], temperature) for name, route in self.paths.items()
        }

    def returns(self, temperatures, water_rate):
        energy = defaultdict(float)
        flow = defaultdict(float)
        steps = {}
        for emitter, route in self.paths.items():
            if not self.flows[emitter]:
                continue
            end = route["return"][-1][2]
            energy[end] += temperatures[emitter] * self.flows[emitter]
            flow[end] += self.flows[emitter]
            for depth, (pipe, first, last) in enumerate(route["return"]):
                steps[pipe] = depth, first, last
        for pipe, (_, first, last) in sorted(steps.items(), key=lambda item: -item[1][0]):
            if flow[last]:
                outlet = self._advance_segment(
                    pipe, flow[last], energy[last] / flow[last], water_rate, last, first
                )
                energy[first] += outlet * flow[last]
                flow[first] += flow[last]

    def finish(self):
        for name in self.pipes.keys() - self.updated:
            self._advance_segment(name, 0.0, self.network.water_c[name], 0.0)

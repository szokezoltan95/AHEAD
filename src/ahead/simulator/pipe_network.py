"""Heat exchange along drawn branches of the existing flow-capped model."""

from collections import defaultdict
from math import expm1


class PipeNetwork:
    def __init__(self, pipes, paths):
        self.pipes = pipes
        self.paths = paths
        self.details = {}

    def frame(self, circuit, flows, air, fabric, outdoor, ground):
        paths = {name: self.paths[name] for name in flows if name in self.paths}
        pipes = {name: pipe for name, pipe in self.pipes.items() if pipe.circuit == circuit}
        return PipeFrame(pipes, paths, flows, air, fabric, outdoor, ground)


class PipeFrame:
    def __init__(self, pipes, paths, flows, air, fabric, outdoor, ground):
        self.pipes = pipes
        self.paths = paths
        self.flows = flows
        self.ambient = {
            name: (fabric.get(pipe.room, ground) if pipe.mounting in ("in_wall", "underfloor")
                   else air.get(pipe.room, outdoor))
            for name, pipe in pipes.items()
        }
        self.details = {
            name: {
                "circuit": pipe.circuit, "run": pipe.run, "mounting": pipe.mounting, "wall": pipe.wall,
                "room": pipe.room, "length_m": pipe.length_m, "flow_l_min": 0.0,
                "inlet_c": None, "outlet_c": None, "heat_loss_kw": 0.0, "ambient_c": self.ambient[name],
                "diameter_mm": pipe.diameter_mm, "insulation_mm": pipe.insulation_mm,
                "offset_mm": pipe.offset_mm, "points": pipe.points,
            } for name, pipe in pipes.items()
        }
        self.air_heat = defaultdict(float)
        self.fabric_heat = defaultdict(float)
        self.loss_kw = 0.0

    def _transfer(self, pipe_id, flow, inlet, water_rate):
        pipe = self.pipes[pipe_id]
        loss = flow * water_rate * -expm1(-pipe.kw_k / (flow * water_rate)) * (
            inlet - self.ambient[pipe_id]
        )
        outlet = inlet - loss / (flow * water_rate)
        self.loss_kw += loss
        if pipe.room:
            target = self.fabric_heat if pipe.mounting in ("in_wall", "underfloor") else self.air_heat
            target[pipe.room] += loss
        self.details[pipe_id].update(
            flow_l_min=flow, inlet_c=inlet, outlet_c=outlet, heat_loss_kw=loss,
        )
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
                at_node[last] = self._transfer(pipe, demand[pipe], at_node.get(first, temperature), water_rate)
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
                outlet = self._transfer(pipe, flow[last], energy[last] / flow[last], water_rate)
                energy[first] += outlet * flow[last]
                flow[first] += flow[last]

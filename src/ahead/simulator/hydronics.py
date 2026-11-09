"""Water circuits, radiators, and floor slabs."""

from dataclasses import dataclass
from math import expm1
from typing import Mapping


WATER_KW_PER_K_PER_L_MIN = 4.186 / 60


@dataclass(frozen=True)
class WaterLoop:
    capacity: float
    initial_c: float
    target_c: float
    loss_kw_k: float = 0
    max_flow_l_min: float = 20
    dhw: bool = False
    mains_c: float = 10
    expose_sensors: bool = True


@dataclass(frozen=True)
class Emitter:
    zone: str
    loop: str
    kw_k: float
    flow_l_min: float
    slab_capacity: float = 0
    slab_to_room_kw_k: float = 0
    travel_s: float = 0
    local_sensor_warming_c: float = 1.5
    local_sensor_bias_c: float = 0.0
    report_valve_opening: bool = False
    fixed_open: bool = False


@dataclass(frozen=True)
class HeatExchanger:
    source: str
    destination: str
    kw_k: float
    max_heat_kw: float
    pump_kw: float = 0


class Hydronics:
    def __init__(self, loops: Mapping[str, WaterLoop], emitters: Mapping[str, Emitter],
                 initial_air: Mapping[str, float], pipe_network=None):
        self.loops = dict(loops)
        self.emitters = dict(emitters)
        self.water = {name: loop.initial_c for name, loop in loops.items()}
        self.slab = {
            name: initial_air[emitter.zone] for name, emitter in emitters.items()
            if emitter.slab_capacity
        }
        self.valves = {name: float(emitter.fixed_open) for name, emitter in emitters.items()}
        self.circuits = {}
        self.details = {}
        self.pipe_network = pipe_network
        self.pipe_air_heat = {}
        self.pipe_fabric_heat = {}
        self._by_loop = {name: [] for name in loops}
        for name, emitter in emitters.items():
            self._by_loop[emitter.loop].append(name)

    def rates(self):
        pipe_rates = dict.fromkeys(self.loops, 0.0)
        if self.pipe_network:
            for pipe in self.pipe_network.pipes.values():
                pipe_rates[pipe.circuit] += pipe.kw_k
        for name, loop in self.loops.items():
            yield (
                pipe_rates[name] + loop.loss_kw_k + sum(self.emitters[key].kw_k for key in self._by_loop[name])
            ) / loop.capacity
        for emitter in self.emitters.values():
            if emitter.slab_capacity:
                yield (emitter.kw_k + emitter.slab_to_room_kw_k) / emitter.slab_capacity

    def exchange(self, air, valves, dt_s, *, fabric=None, outdoor=0, ground=0, pump_enabled=None):
        water_heat = dict.fromkeys(self.loops, 0.0)
        room_heat = dict.fromkeys(air, 0.0)
        slab_heat = {}
        self.pipe_air_heat = dict.fromkeys(air, 0.0)
        self.pipe_fabric_heat = dict.fromkeys(air, 0.0)
        for name, emitter in self.emitters.items():
            request = valves.get(name, 0.0)
            if emitter.fixed_open:
                self.valves[name] = 1.0
            elif emitter.travel_s:
                maximum_move = dt_s / emitter.travel_s
                difference = request - self.valves[name]
                self.valves[name] += max(-maximum_move, min(maximum_move, difference))
            else:
                self.valves[name] = request
        pump_enabled = pump_enabled or {}
        for loop_name, loop in self.loops.items():
            supply = self.water[loop_name]
            names = self._by_loop[loop_name]
            requested_flow = sum(self.emitters[name].flow_l_min * self.valves[name] for name in names)
            flow = min(requested_flow, loop.max_flow_l_min) if pump_enabled.get(loop_name, True) else 0.0
            scale = flow / requested_flow if requested_flow else 0
            flows = {name: self.valves[name] * self.emitters[name].flow_l_min * scale for name in names}
            pipe_frame = None
            supply_by_emitter = {}
            return_by_emitter = {}
            if self.pipe_network:
                pipe_frame = self.pipe_network.frame(loop_name, flows, air, fabric or air, outdoor, ground)
                supply_by_emitter = pipe_frame.supply(supply, WATER_KW_PER_K_PER_L_MIN)
            for name in names:
                emitter = self.emitters[name]
                zone_c = air[emitter.zone]
                emitter_c = self.slab.get(name, zone_c)
                branch = flows[name]
                inlet = supply_by_emitter.get(name, supply)
                rate = branch * WATER_KW_PER_K_PER_L_MIN
                extracted = 0.0
                if rate:
                    effectiveness = -expm1(-emitter.kw_k / rate)
                    extracted = rate * effectiveness * max(0.0, inlet - emitter_c)
                water_heat[loop_name] -= extracted
                delivered = extracted
                if name in self.slab:
                    delivered = (emitter_c - zone_c) * emitter.slab_to_room_kw_k
                    slab_heat[name] = extracted - delivered
                room_heat[emitter.zone] += delivered
                outlet = inlet - extracted / rate if rate else None
                return_by_emitter[name] = outlet
                self.details[name] = {
                    "zone": emitter.zone, "loop": loop_name, "fixed_open": emitter.fixed_open,
                    "request": 1.0 if emitter.fixed_open else valves.get(name, 0),
                    "opening": self.valves[name], "flow_l_min": branch, "supply_c": inlet,
                    "return_c": outlet, "drawn_route": name in supply_by_emitter,
                    "water_heat_kw": extracted, "room_heat_kw": delivered,
                    "slab_c": self.slab.get(name),
                }
            if pipe_frame:
                pipe_frame.returns(return_by_emitter, WATER_KW_PER_K_PER_L_MIN)
                water_heat[loop_name] -= pipe_frame.loss_kw
                for name, heat in pipe_frame.air_heat.items():
                    self.pipe_air_heat[name] += heat
                for name, heat in pipe_frame.fabric_heat.items():
                    self.pipe_fabric_heat[name] += heat
                self.pipe_network.details.update(pipe_frame.details)
            self.circuits[loop_name] = {
                "supply_c": supply,
                "return_c": supply + water_heat[loop_name] / (
                    flow * WATER_KW_PER_K_PER_L_MIN
                ) if flow else None,
                "flow_l_min": flow, "pump_on": bool(flow and pump_enabled.get(loop_name, True)),
            }
        return water_heat, room_heat, slab_heat

    def advance(self, dt_h, water_heat, slab_heat):
        for name, heat in water_heat.items():
            self.water[name] += heat * dt_h / self.loops[name].capacity
        for name, heat in slab_heat.items():
            self.slab[name] += heat * dt_h / self.emitters[name].slab_capacity

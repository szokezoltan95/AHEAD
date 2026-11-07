"""Thermal network for rooms, fabric, shared hydronics, and heat sources.

Heat flows are in kW and thermal capacities are in kWh/K. Each heat transfer
between simulated nodes enters one node and leaves the other in the same step.
"""

from dataclasses import dataclass
from math import expm1, isfinite
from typing import Mapping

from ahead.control import ControlPlan, Equipment, SourceCapabilities


# Approximate heat capacity rate of water at a flow of 1 L/min.
WATER_KW_PER_K_PER_L_MIN = 4.186 / 60


@dataclass(frozen=True)
class Zone:
    name: str
    air_capacity: float
    fabric_capacity: float
    air_fabric_kw_k: float
    fabric_outside_kw_k: float
    infiltration_kw_k: float
    initial_c: float
    setpoint_c: float
    solar_area_m2: float = 0
    radiator_kw_k: float = 0
    radiator_flow_l_min: float = 0
    base_gains_kw: float = 0


@dataclass(frozen=True)
class Partition:
    first: str
    second: str
    kw_k: float


@dataclass(frozen=True)
class WaterLoop:
    capacity: float
    initial_c: float
    target_c: float
    loss_kw_k: float = 0
    max_flow_l_min: float = 20


@dataclass(frozen=True)
class Source:
    name: str
    fuel: str
    max_heat_kw: float
    efficiency: float
    direct_zone: str | None = None
    modulating: bool = False
    efficiency_per_outdoor_k: float = 0
    reference_outdoor_c: float = 7
    min_on_s: float = 0
    min_off_s: float = 0


@dataclass(frozen=True)
class Conditions:
    outdoor_c: float
    solar_kw_m2: float = 0
    occupied_gains_kw: float = 0
    appliance_kw: float = 0
    electricity_price_kwh: float = 0
    gas_price_kwh: float = 0


@dataclass
class Totals:
    electricity_kwh: float = 0
    gas_kwh: float = 0
    generated_heat_kwh: float = 0
    cost: float = 0
    comfort_degree_hours: float = 0


@dataclass
class _SourceState:
    on: bool = False
    changed_at_s: float = float("-inf")
    starts: int = 0


def _check(values, *, positive=False):
    if any(not isfinite(value) or value < 0 or (positive and value == 0) for value in values):
        raise ValueError("Thermal parameters must be finite and within their allowed range")


class House:
    def __init__(
        self,
        zones: tuple[Zone, ...],
        partitions: tuple[Partition, ...],
        sources: tuple[Source, ...],
        loop: WaterLoop | None = None,
    ):
        self.zones = {zone.name: zone for zone in zones}
        self.sources = {source.name: source for source in sources}
        if not zones or len(self.zones) != len(zones) or len(self.sources) != len(sources):
            raise ValueError("A house needs uniquely named zones and sources")
        self._partitions = partitions
        self._loop = loop
        self._air = {zone.name: zone.initial_c for zone in zones}
        self._fabric = self._air.copy()
        self._water = loop.initial_c if loop else None
        self.return_c: float | None = None
        self.flow_l_min = 0.0
        self._source_state = {name: _SourceState() for name in self.sources}
        self._source_heat_kw = dict.fromkeys(self.sources, 0.0)
        self.elapsed_s = 0.0
        self.totals = Totals()

        air_conductance = {}
        radiator_zones = frozenset(zone.name for zone in zones if zone.radiator_kw_k)
        for zone in zones:
            _check((zone.air_capacity, zone.fabric_capacity), positive=True)
            _check((zone.air_fabric_kw_k, zone.fabric_outside_kw_k, zone.infiltration_kw_k,
                    zone.solar_area_m2, zone.radiator_kw_k, zone.radiator_flow_l_min,
                    zone.base_gains_kw))
            if zone.radiator_kw_k and not zone.radiator_flow_l_min:
                raise ValueError("Radiators need a positive design flow")
            if not all(map(isfinite, (zone.initial_c, zone.setpoint_c))):
                raise ValueError("Zone temperatures must be finite")
            air_conductance[zone.name] = (
                zone.air_fabric_kw_k + zone.infiltration_kw_k + zone.radiator_kw_k
            )
        for link in partitions:
            _check((link.kw_k,), positive=True)
            if link.first == link.second or any(
                name not in self.zones for name in (link.first, link.second)
            ):
                raise ValueError("A partition must connect two distinct known zones")
            air_conductance[link.first] += link.kw_k
            air_conductance[link.second] += link.kw_k
        if loop:
            _check((loop.capacity, loop.max_flow_l_min), positive=True)
            _check((loop.loss_kw_k,))
            if not all(map(isfinite, (loop.initial_c, loop.target_c))):
                raise ValueError("Water temperatures must be finite")
        elif radiator_zones:
            raise ValueError("Radiators require a water loop")
        for source in sources:
            _check((source.max_heat_kw, source.efficiency), positive=True)
            _check((source.min_on_s, source.min_off_s))
            if not all(map(isfinite, (source.reference_outdoor_c, source.efficiency_per_outdoor_k))):
                raise ValueError("Source efficiency curve must be finite")
            if source.fuel not in {"gas", "electricity"}:
                raise ValueError("Source fuel must be gas or electricity")
            if source.fuel == "gas" and (
                source.efficiency > 1 or source.efficiency_per_outdoor_k != 0
            ):
                raise ValueError("Gas sources require a fixed efficiency of at most one")
            if source.direct_zone is None and not loop:
                raise ValueError("A hydronic heat source requires a water loop")
            if source.direct_zone is not None and source.direct_zone not in self.zones:
                raise ValueError("Direct heat source references an unknown zone")
        self.equipment = Equipment(
            {
                source.name: SourceCapabilities(
                    radiator_zones if source.direct_zone is None else frozenset({source.direct_zone}),
                    modulating=source.modulating,
                    uses_water_loop=source.direct_zone is None,
                )
                for source in sources
            },
            radiator_zones,
        )

        # Bound each explicit integration step by the fastest thermal node.
        max_rate = max(
            max(air_conductance[z.name] / z.air_capacity,
                (z.air_fabric_kw_k + z.fabric_outside_kw_k) / z.fabric_capacity)
            for z in zones
        )
        if loop:
            water_rate = (sum(z.radiator_kw_k for z in zones) + loop.loss_kw_k) / loop.capacity
            max_rate = max(max_rate, water_rate)
        self._max_step_s = min(30.0, 900 / max_rate) if max_rate else 30.0

    @property
    def indoor_c(self) -> dict[str, float]:
        return self._air.copy()

    @property
    def water_c(self) -> float | None:
        return self._water

    @property
    def source_starts(self) -> dict[str, int]:
        return {name: state.starts for name, state in self._source_state.items()}

    @property
    def source_heat_kw(self) -> dict[str, float]:
        return self._source_heat_kw.copy()

    def advance(
        self,
        duration_s: float,
        conditions: Conditions,
        plan: ControlPlan,
        available_sources: frozenset[str],
    ) -> None:
        _check((duration_s,), positive=True)
        _check((conditions.solar_kw_m2, conditions.occupied_gains_kw, conditions.appliance_kw))
        if not all(map(isfinite, (conditions.outdoor_c, conditions.electricity_price_kwh,
                                  conditions.gas_price_kwh))):
            raise ValueError("Weather and tariffs must be finite")
        if plan.sources.keys() - self.sources.keys() or plan.valves.keys() - self.equipment.valves:
            raise ValueError("Control plan references an unknown actuator")
        for fraction in (*plan.sources.values(), *plan.valves.values()):
            if not 0 <= fraction <= 1:
                raise ValueError("Actuator commands must be between zero and one")
        for name, fraction in plan.sources.items():
            if 0 < fraction < 1 and not self.sources[name].modulating:
                raise ValueError(f"Source {name} only accepts on/off commands")

        remaining_s = duration_s
        while remaining_s > 0:
            step_s = min(remaining_s, self._max_step_s)
            self._step(step_s / 3600, conditions, plan, available_sources)
            self.elapsed_s += step_s
            remaining_s -= step_s
        self._radiator_output(plan)

    def _step(self, dt_h, conditions, plan, available_sources):
        air_flow = dict.fromkeys(self.zones, 0.0)
        fabric_flow = dict.fromkeys(self.zones, 0.0)
        water_flow = 0.0
        electricity_kw = conditions.appliance_kw
        gas_kw = heat_kw = 0.0
        outdoor = conditions.outdoor_c
        appliance_gains = (conditions.occupied_gains_kw + conditions.appliance_kw) / len(self.zones)

        radiators = self._radiator_output(plan)
        for name, zone in self.zones.items():
            air = self._air[name]
            fabric = self._fabric[name]
            coupling = (fabric - air) * zone.air_fabric_kw_k
            air_flow[name] = coupling + (outdoor - air) * zone.infiltration_kw_k
            air_flow[name] += zone.base_gains_kw + appliance_gains
            fabric_flow[name] = -coupling + (outdoor - fabric) * zone.fabric_outside_kw_k
            fabric_flow[name] += conditions.solar_kw_m2 * zone.solar_area_m2
            radiator = radiators.get(name, 0.0)
            water_flow -= radiator
            air_flow[name] += radiator

        for link in self._partitions:
            flow = (self._air[link.second] - self._air[link.first]) * link.kw_k
            air_flow[link.first] += flow
            air_flow[link.second] -= flow

        if self._loop and self._water is not None:
            water_flow -= (self._water - outdoor) * self._loop.loss_kw_k

        for name, source in self.sources.items():
            request = plan.sources.get(name, 0.0)
            if source.direct_zone is None:
                assert self._loop is not None and self._water is not None
                headroom = (self._loop.target_c - self._water) * self._loop.capacity / dt_h
                limit_kw = max(0.0, headroom - water_flow)
            else:
                limit_kw = source.max_heat_kw
            requested_kw = min(request * source.max_heat_kw, limit_kw)
            delivered = self._source_output(source, requested_kw, limit_kw, name in available_sources)
            self._source_heat_kw[name] = delivered
            if source.direct_zone is None:
                water_flow += delivered
            else:
                air_flow[source.direct_zone] += delivered
            efficiency = max(
                0.1, source.efficiency
                + source.efficiency_per_outdoor_k * (outdoor - source.reference_outdoor_c)
            )
            if source.fuel == "gas":
                gas_kw += delivered / efficiency
            else:
                electricity_kw += delivered / efficiency
            heat_kw += delivered

        for name, zone in self.zones.items():
            before = self._air[name] - zone.setpoint_c
            self._air[name] += air_flow[name] * dt_h / zone.air_capacity
            self._fabric[name] += fabric_flow[name] * dt_h / zone.fabric_capacity
            after = self._air[name] - zone.setpoint_c
            area = (abs(before) + abs(after)) * dt_h / 2
            if before * after < 0:
                area *= (before**2 + after**2) / (after - before)**2
            self.totals.comfort_degree_hours += area
        if self._loop and self._water is not None:
            self._water += water_flow * dt_h / self._loop.capacity
        self.totals.electricity_kwh += electricity_kw * dt_h
        self.totals.gas_kwh += gas_kw * dt_h
        self.totals.generated_heat_kwh += heat_kw * dt_h
        self.totals.cost += (
            electricity_kw * conditions.electricity_price_kwh + gas_kw * conditions.gas_price_kwh
        ) * dt_h

    def _radiator_output(self, plan: ControlPlan) -> dict[str, float]:
        if self._loop is None or self._water is None:
            return {}
        requested = {
            name: zone.radiator_flow_l_min * plan.valves.get(name, 0.0)
            for name, zone in self.zones.items() if zone.radiator_kw_k
        }
        total_flow = sum(requested.values())
        self.flow_l_min = min(total_flow, self._loop.max_flow_l_min)
        if not total_flow:
            self.return_c = None
            return {}

        scale = self.flow_l_min / total_flow
        outputs = {}
        for name, flow in requested.items():
            if flow:
                capacity_rate = flow * scale * WATER_KW_PER_K_PER_L_MIN
                effectiveness = -expm1(-self.zones[name].radiator_kw_k / capacity_rate)
                outputs[name] = capacity_rate * effectiveness * max(
                    0.0, self._water - self._air[name]
                )
        self.return_c = self._water - sum(outputs.values()) / (
            self.flow_l_min * WATER_KW_PER_K_PER_L_MIN
        )
        return outputs

    def _source_output(self, source, requested_kw, limit_kw, available):
        state = self._source_state[source.name]
        since_change = self.elapsed_s - state.changed_at_s
        if not available or limit_kw <= 0:
            output = 0.0
        elif state.on and not requested_kw and since_change < source.min_on_s:
            output = min(source.max_heat_kw, limit_kw)
        elif not state.on and since_change < source.min_off_s:
            output = 0.0
        else:
            output = requested_kw
        on = output > 0
        if on != state.on:
            state.on = on
            state.changed_at_s = self.elapsed_s
            state.starts += int(on)
        return output

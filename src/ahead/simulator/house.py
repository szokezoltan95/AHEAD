"""Thermal network for rooms, fabric, shared hydronics, and heat sources.

Heat flows are in kW and thermal capacities are in kWh/K. Each heat transfer
between simulated nodes enters one node and leaves the other in the same step.
"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping

from ahead.control import ControlPlan, Equipment, SourceCapabilities


from ahead.simulator.hydronics import Emitter, HeatExchanger, Hydronics, WaterLoop, WATER_KW_PER_K_PER_L_MIN


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
    ground_kw_k: float = 0
    conditioned: bool = True
    expose_sensor: bool = True


@dataclass(frozen=True)
class Partition:
    first: str
    second: str
    kw_k: float


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
    loop: str = 'main'
    reversible: bool = False
    outdoor_unit: str | None = None
    efficiency_per_water_k: float = 0
    min_fraction: float = 0
    room_setpoint_control: bool = False
    fan_control: bool = False
    water_setpoint_control: bool = False
    dhw_loop: str | None = None
    report_heat: bool = True


@dataclass(frozen=True)
class OutdoorUnit:
    capacity_kw: float
    cop: float
    cop_per_outdoor_k: float = 0
    reference_c: float = 7
    capacity_per_outdoor_k: float = 0
    defrost_period_s: int = 0
    defrost_duration_s: int = 0
    defrost_below_c: float = 3
    defrost_power_kw: float = 0
    mode_rule: str = "largest_demand"
    allocation: str = "proportional"


@dataclass(frozen=True)
class Conditions:
    outdoor_c: float
    solar_kw_m2: float = 0
    occupied_gains_kw: float = 0
    appliance_kw: float = 0
    electricity_price_kwh: float = 0
    gas_price_kwh: float = 0
    forecast_outdoor_c: float | None = None
    ground_c: float = 10
    solar_by_zone_kw: Mapping[str, float] = field(default_factory=dict)
    ventilation_kw_k: Mapping[str, float] = field(default_factory=dict)
    door_exchange_kw_k: Mapping[tuple[str, str], float] = field(default_factory=dict)
    solar_water_kw: Mapping[str, float] = field(default_factory=dict)
    solar_thermal: Mapping[str, tuple[str, float, float]] = field(default_factory=dict)
    solar_pv_kw: float = 0
    dhw_draw_l_min: Mapping[str, float] = field(default_factory=dict)


@dataclass
class Totals:
    electricity_kwh: float = 0
    gas_kwh: float = 0
    generated_heat_kwh: float = 0
    cost: float = 0
    comfort_degree_hours: float = 0
    cooling_kwh: float = 0
    pv_kwh: float = 0
    import_kwh: float = 0
    export_kwh: float = 0


@dataclass
class _SourceState:
    on: bool = False
    changed_at_s: float = float("-inf")
    starts: int = 0
    output_kw: float = 0


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
        *,
        loops: Mapping[str, WaterLoop] | None = None,
        emitters: Mapping[str, Emitter] | None = None,
        outdoor_units: Mapping[str, OutdoorUnit] | None = None,
        exchangers: Mapping[str, HeatExchanger] | None = None,
    ):
        self.zones = {zone.name: zone for zone in zones}
        self.sources = {source.name: source for source in sources}
        if not zones or len(self.zones) != len(zones) or len(self.sources) != len(sources):
            raise ValueError("A house needs uniquely named zones and sources")
        self._partitions = partitions
        self._air = {zone.name: zone.initial_c for zone in zones}
        self._fabric = self._air.copy()
        loops = dict(loops or ({"main": loop} if loop else {}))
        emitters = dict(emitters) if emitters is not None else {
            zone.name: Emitter(zone.name, "main", zone.radiator_kw_k, zone.radiator_flow_l_min)
            for zone in zones if zone.radiator_kw_k
        }
        self.outdoor_units = dict(outdoor_units or {})
        self.exchangers = dict(exchangers or {})
        self._exchanger_details = {}
        self._room_flux = {}
        self._source_details = {}
        self._outdoor_details = {}
        self._balance = {}
        self._dhw_charging: dict[str, bool] = {}
        self._source_state = {name: _SourceState() for name in self.sources}
        self._source_heat_kw = dict.fromkeys(self.sources, 0.0)
        self.elapsed_s = 0.0
        self.totals = Totals()

        air_conductance = {}
        for zone in zones:
            _check((zone.air_capacity, zone.fabric_capacity), positive=True)
            _check((zone.air_fabric_kw_k, zone.fabric_outside_kw_k, zone.infiltration_kw_k,
                    zone.solar_area_m2, zone.radiator_kw_k, zone.radiator_flow_l_min,
                    zone.base_gains_kw, zone.ground_kw_k))
            if zone.radiator_kw_k and not zone.radiator_flow_l_min:
                raise ValueError("Radiators need a positive design flow")
            if not all(map(isfinite, (zone.initial_c, zone.setpoint_c))):
                raise ValueError("Zone temperatures must be finite")
            air_conductance[zone.name] = (
                zone.air_fabric_kw_k + zone.infiltration_kw_k
            )
        for link in partitions:
            _check((link.kw_k,), positive=True)
            if link.first == link.second or any(
                name not in self.zones for name in (link.first, link.second)
            ):
                raise ValueError("A partition must connect two distinct known zones")
            air_conductance[link.first] += link.kw_k
            air_conductance[link.second] += link.kw_k
        for loop in loops.values():
            _check((loop.capacity, loop.max_flow_l_min), positive=True)
            _check((loop.loss_kw_k,))
            if not all(map(isfinite, (loop.initial_c, loop.target_c, loop.mains_c))):
                raise ValueError("Water temperatures must be finite")
        for name, emitter in emitters.items():
            if emitter.zone not in self.zones or emitter.loop not in loops:
                raise ValueError(f"Emitter {name} must connect to a room and water circuit")
            _check((emitter.kw_k, emitter.flow_l_min), positive=True)
            _check((emitter.slab_capacity, emitter.slab_to_room_kw_k, emitter.travel_s))
            air_conductance[emitter.zone] += emitter.slab_to_room_kw_k or emitter.kw_k
        self.hydronics = Hydronics(loops, emitters, self._air)
        self._first_loop = next(iter(loops), None)
        exchanger_rates = dict.fromkeys(loops, 0.0)
        for name, exchanger in self.exchangers.items():
            if exchanger.source == exchanger.destination or any(
                key not in loops for key in (exchanger.source, exchanger.destination)
            ):
                raise ValueError(f"Heat exchanger {name} requires two different circuits")
            _check((exchanger.kw_k, exchanger.max_heat_kw), positive=True)
            _check((exchanger.pump_kw,))
            for key in (exchanger.source, exchanger.destination):
                exchanger_rates[key] += exchanger.kw_k / loops[key].capacity
        for unit in self.outdoor_units.values():
            _check((unit.capacity_kw, unit.cop), positive=True)
            _check((unit.defrost_period_s, unit.defrost_duration_s, unit.defrost_power_kw))
            if unit.mode_rule not in {"largest_demand", "heat_priority", "cool_priority"} or unit.allocation not in {"proportional", "in_order"}:
                raise ValueError("Unsupported shared heat-pump mode or allocation rule")
            if unit.defrost_duration_s > unit.defrost_period_s:
                raise ValueError("Defrost duration cannot exceed its period")
        for source in sources:
            _check((source.max_heat_kw, source.efficiency), positive=True)
            _check((source.min_on_s, source.min_off_s, source.min_fraction))
            if source.min_fraction > 1:
                raise ValueError("Minimum output fraction cannot exceed one")
            if source.dhw_loop and (source.dhw_loop not in loops or not loops[source.dhw_loop].dhw):
                raise ValueError("Domestic hot-water priority requires a hot-water cylinder")
            if source.outdoor_unit is not None and source.outdoor_unit not in self.outdoor_units:
                raise ValueError("Indoor unit must connect to a known outdoor unit")
            if not all(map(isfinite, (source.reference_outdoor_c, source.efficiency_per_outdoor_k))):
                raise ValueError("Source efficiency curve must be finite")
            if source.fuel not in {"gas", "electricity"}:
                raise ValueError("Source fuel must be gas or electricity")
            if source.fuel == "gas" and (
                source.efficiency > 1 or source.efficiency_per_outdoor_k != 0
            ):
                raise ValueError("Gas sources require a fixed efficiency of at most one")
            if source.direct_zone is None and source.loop not in loops:
                raise ValueError("A hydronic heat source requires a water loop")
            if source.direct_zone is not None and source.direct_zone not in self.zones:
                raise ValueError("Direct heat source references an unknown zone")
        self.equipment = Equipment(
            {
                source.name: SourceCapabilities(
                    frozenset(e.zone for e in emitters.values() if e.loop == source.loop)
                    if source.direct_zone is None else frozenset({source.direct_zone}),
                    modulating=source.modulating,
                    uses_water_loop=source.direct_zone is None,
                    reversible=source.reversible,
                )
                for source in sources
            },
            frozenset(emitters),
            {name: emitter.zone for name, emitter in emitters.items()},
        )

        # Bound each explicit integration step by the fastest thermal node.
        max_rate = max(
            max(air_conductance[z.name] / z.air_capacity,
                (z.air_fabric_kw_k + z.fabric_outside_kw_k + z.ground_kw_k) / z.fabric_capacity)
            for z in zones
        )
        self._max_rate = max((max_rate, *self.hydronics.rates())) + max(exchanger_rates.values(), default=0)

    def reset_run_clock(self) -> None:
        """Preserve thermal state while starting a new measurement window."""
        self.elapsed_s = 0.0
        self.totals = Totals()
        self._source_state = {name: _SourceState() for name in self.sources}
        self._source_heat_kw = dict.fromkeys(self.sources, 0.0)
        self.hydronics.valves = dict.fromkeys(self.hydronics.emitters, 0.0)
        self._source_details.clear()
        self._outdoor_details.clear()
        self._balance.clear()
        self._dhw_charging.clear()

    def checkpoint(self) -> dict:
        return {
            "air_c": self._air.copy(), "fabric_c": self._fabric.copy(),
            "water_c": self.hydronics.water.copy(), "slab_c": self.hydronics.slab.copy(),
        }

    def restore_checkpoint(self, snapshot: dict) -> None:
        for key, target in (("air_c", self._air), ("fabric_c", self._fabric),
                            ("water_c", self.hydronics.water), ("slab_c", self.hydronics.slab)):
            values = snapshot[key]
            if target.keys() != values.keys() or not all(map(isfinite, values.values())):
                raise ValueError("Checkpoint does not match this house")
            target.update(values)
        self.reset_run_clock()

    @property
    def indoor_c(self) -> dict[str, float]:
        return self._air.copy()

    @property
    def water_c(self) -> float | None:
        return self.hydronics.water.get(self._first_loop)

    @property
    def return_c(self) -> float | None:
        return self.hydronics.circuits.get(self._first_loop, {}).get("return_c")

    @property
    def flow_l_min(self) -> float:
        return self.hydronics.circuits.get(self._first_loop, {}).get("flow_l_min", 0.0)

    def diagnostics(self) -> dict:
        return {
            "rooms": {
                name: {"air_c": self._air[name], "fabric_c": self._fabric[name],
                       **self._room_flux.get(name, {})}
                for name in self.zones
            },
            "circuits": {
                name: {**self.hydronics.circuits.get(name, {}), "supply_c": temperature,
                       "target_c": self.hydronics.loops[name].target_c}
                for name, temperature in self.hydronics.water.items()
            },
            "emitters": self.hydronics.details,
            "sources": self._source_details,
            "outdoor_units": self._outdoor_details,
            "heat_exchangers": self._exchanger_details,
            "balance": self._balance,
        }

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
        _check((conditions.solar_kw_m2, conditions.occupied_gains_kw, conditions.appliance_kw,
                conditions.solar_pv_kw, *conditions.ventilation_kw_k.values(),
                *conditions.door_exchange_kw_k.values(), *conditions.dhw_draw_l_min.values()))
        if not all(map(isfinite, (conditions.outdoor_c, conditions.electricity_price_kwh,
                                  conditions.gas_price_kwh))):
            raise ValueError("Weather and tariffs must be finite")
        if plan.sources.keys() - self.sources.keys() or plan.valves.keys() - self.equipment.valves:
            raise ValueError("Control plan references an unknown actuator")
        for fraction in plan.valves.values():
            if not 0 <= fraction <= 1:
                raise ValueError("Valve commands must be between zero and one")
        for name, fraction in plan.sources.items():
            source = self.sources[name]
            if not (-1 if source.reversible else 0) <= fraction <= 1:
                raise ValueError(f"Invalid request for {name}")
            if not source.modulating and fraction not in (0, 1):
                raise ValueError(f"Source {name} only accepts on/off commands")

        for values, capability, lower, upper in (
            (plan.room_setpoints_c, "room_setpoint_control", 10, 35),
            (plan.fan_fractions, "fan_control", 0, 1),
            (plan.water_setpoints_c, "water_setpoint_control", 20, 80),
        ):
            for name, value in values.items():
                if name not in self.sources or not getattr(self.sources[name], capability) or not lower <= value <= upper:
                    raise ValueError(f"Invalid {capability} request for {name}")
        dynamic_rate = {
            name: conditions.ventilation_kw_k.get(name, 0.0) for name in self.zones
        }
        for (first, second), conductance in conditions.door_exchange_kw_k.items():
            dynamic_rate[first] += conductance
            dynamic_rate[second] += conductance
        max_rate = self._max_rate + max(
            dynamic_rate[name] / zone.air_capacity for name, zone in self.zones.items()
        )
        max_rate += max((
            rate * WATER_KW_PER_K_PER_L_MIN / self.hydronics.loops[name].capacity
            for name, rate in conditions.dhw_draw_l_min.items()
        ), default=0)
        max_step_s = min(30.0, 900 / max_rate) if max_rate else 30.0
        remaining_s = duration_s
        while remaining_s > 0:
            step_s = min(remaining_s, max_step_s)
            self._step(step_s / 3600, conditions, plan, available_sources)
            self.elapsed_s += step_s
            remaining_s -= step_s
        self.hydronics.exchange(self._air, plan.valves, 0)

    def _step(self, dt_h, conditions, plan, available_sources):
        air_flow = dict.fromkeys(self.zones, 0.0)
        fabric_flow = dict.fromkeys(self.zones, 0.0)
        water_flow, emitter_heat, slab_heat = self.hydronics.exchange(
            self._air, plan.valves, dt_h * 3600
        )
        electricity_kw = conditions.appliance_kw
        gas_kw = heat_kw = 0.0
        outdoor = conditions.outdoor_c
        appliance_gains = (conditions.occupied_gains_kw + conditions.appliance_kw) / len(self.zones)

        for name, zone in self.zones.items():
            air = self._air[name]
            fabric = self._fabric[name]
            coupling = (fabric - air) * zone.air_fabric_kw_k
            ventilation = (outdoor - air) * (
                zone.infiltration_kw_k + conditions.ventilation_kw_k.get(name, 0)
            )
            shell = (outdoor - fabric) * zone.fabric_outside_kw_k
            ground = (conditions.ground_c - fabric) * zone.ground_kw_k
            gains = zone.base_gains_kw + appliance_gains
            sun = conditions.solar_kw_m2 * zone.solar_area_m2 + conditions.solar_by_zone_kw.get(name, 0)
            air_flow[name] = coupling + ventilation + gains + emitter_heat[name]
            fabric_flow[name] = -coupling + shell + ground + sun
            self._room_flux[name] = {
                "fabric_to_air_kw": coupling, "ventilation_kw": ventilation, "envelope_kw": shell,
                "ground_kw": ground, "internal_gains_kw": gains, "solar_kw": sun, "emitters_kw": emitter_heat[name],
                "neighbours_kw": 0.0, "direct_heat_kw": 0.0,
            }

        transfers = [(link.first, link.second, link.kw_k) for link in self._partitions]
        transfers.extend((a, b, value) for (a, b), value in conditions.door_exchange_kw_k.items())
        for first, second, conductance in transfers:
            flow = (self._air[second] - self._air[first]) * conductance
            air_flow[first] += flow
            air_flow[second] -= flow
            self._room_flux[first]["neighbours_kw"] += flow
            self._room_flux[second]["neighbours_kw"] -= flow

        for name, loop in self.hydronics.loops.items():
            temperature = self.hydronics.water[name]
            loss = (temperature - outdoor) * loop.loss_kw_k
            draw = conditions.dhw_draw_l_min.get(name, 0) * WATER_KW_PER_K_PER_L_MIN
            water_flow[name] -= loss + draw * (temperature - loop.mains_c)
            water_flow[name] += conditions.solar_water_kw.get(name, 0)
        solar_collected = {}
        for name, (loop, optical_kw, loss_kw_k) in conditions.solar_thermal.items():
            gain = max(0.0, optical_kw - loss_kw_k * (
                self.hydronics.water[loop] - outdoor
            )) if optical_kw else 0.0
            tank = self.hydronics.loops[loop]
            headroom = (tank.target_c - self.hydronics.water[loop]) * tank.capacity / dt_h
            gain = min(gain, max(0, headroom - water_flow[loop]))
            solar_collected[name] = gain
            water_flow[loop] += gain

        for name, exchanger in self.exchangers.items():
            source_temperature = self.hydronics.water[exchanger.source]
            destination_temperature = self.hydronics.water[exchanger.destination]
            destination = self.hydronics.loops[exchanger.destination]
            headroom = (destination.target_c - destination_temperature) * destination.capacity / dt_h
            transfer = min(
                exchanger.max_heat_kw, max(0, headroom - water_flow[exchanger.destination]),
                max(0, (source_temperature - destination_temperature) * exchanger.kw_k),
            )
            water_flow[exchanger.source] -= transfer
            water_flow[exchanger.destination] += transfer
            power = exchanger.pump_kw if transfer else 0.0
            electricity_kw += power
            self._exchanger_details[name] = {
                "source_circuit": exchanger.source, "circuit": exchanger.destination,
                "heat_kw": transfer, "pump_kw": power,
            }

        demand = {}
        for name, source in self.sources.items():
            request = plan.sources.get(name, 0)
            target = plan.room_setpoints_c.get(name)
            if target is not None and source.direct_zone and request:
                direction = 1 if request > 0 else -1
                error = (target - self._air[source.direct_zone]) * direction
                request = direction * min(abs(request), max(0, error / 2))
            demand[name] = request * plan.fan_fractions.get(name, 1)
        allocated, overhead_kw = self._outdoor_allocation(demand, available_sources, outdoor)
        electricity_kw += overhead_kw
        for name, source in self.sources.items():
            request = demand[name]
            loop_name = self._source_loop(name, source)
            water_target = None
            limit_kw = source.max_heat_kw
            if source.direct_zone is None:
                loop = self.hydronics.loops[loop_name]
                water_target = loop.target_c
                if loop_name == source.loop:
                    water_target = min(loop.target_c, plan.water_setpoints_c.get(name, loop.target_c))
                headroom = (water_target - self.hydronics.water[loop_name]) * loop.capacity / dt_h
                limit_kw = min(limit_kw, max(0.0, headroom - water_flow[loop_name]))
            if source.outdoor_unit:
                requested_kw = allocated.get(name, 0.0)
                limit_kw = abs(requested_kw)
            elif request < 0:
                requested_kw = -min(abs(request) * source.max_heat_kw, limit_kw)
            elif request:
                requested_kw = min(max(request, source.min_fraction) * source.max_heat_kw, limit_kw)
            else:
                requested_kw = 0.0

            delivered, status = self._source_output(source, requested_kw, limit_kw, name in available_sources)
            self._source_heat_kw[name] = delivered
            if source.direct_zone is None:
                water_flow[loop_name] += delivered
            else:
                air_flow[source.direct_zone] += delivered
                self._room_flux[source.direct_zone]["direct_heat_kw"] += delivered
            efficiency = source.efficiency + source.efficiency_per_outdoor_k * (
                outdoor - source.reference_outdoor_c
            )
            if source.direct_zone is None:
                efficiency += source.efficiency_per_water_k * (water_target - 35)
            if source.outdoor_unit:
                unit = self.outdoor_units[source.outdoor_unit]
                efficiency = unit.cop + unit.cop_per_outdoor_k * (outdoor - unit.reference_c)
            efficiency = max(0.1, efficiency)
            power = abs(delivered) / efficiency
            if source.fuel == "gas":
                gas_kw += power
            else:
                electricity_kw += power
            if source.outdoor_unit:
                self._outdoor_details[source.outdoor_unit]["electrical_kw"] += power
            heat_kw += delivered
            self._source_details[name] = {
                "zone": source.direct_zone, "loop": loop_name if not source.direct_zone else None,
                "outdoor_unit": source.outdoor_unit, "request": plan.sources.get(name, 0),
                "effective_request": request, "heat_kw": delivered, "water_target_c": water_target,
                "room_target_c": plan.room_setpoints_c.get(name),
                "fan_fraction": plan.fan_fractions.get(name),
                "dhw_priority_active": bool(source.dhw_loop and loop_name == source.dhw_loop),
                "input_kw": power, "fuel": source.fuel, "efficiency": efficiency, "status": status,
                "starts": self._source_state[name].starts,
            }

        for name, zone in self.zones.items():
            before = self._air[name] - zone.setpoint_c
            self._air[name] += air_flow[name] * dt_h / zone.air_capacity
            self._fabric[name] += fabric_flow[name] * dt_h / zone.fabric_capacity
            after = self._air[name] - zone.setpoint_c
            area = (abs(before) + abs(after)) * dt_h / 2
            if before * after < 0:
                area *= (before**2 + after**2) / (after - before)**2
            if zone.conditioned:
                self.totals.comfort_degree_hours += area
        self.hydronics.advance(dt_h, water_flow, slab_heat)
        imported = max(0, electricity_kw - conditions.solar_pv_kw)
        exported = max(0, conditions.solar_pv_kw - electricity_kw)
        self.totals.electricity_kwh += electricity_kw * dt_h
        self.totals.gas_kwh += gas_kw * dt_h
        self.totals.generated_heat_kwh += sum(max(0, value) for value in self._source_heat_kw.values()) * dt_h
        self.totals.cooling_kwh += sum(max(0, -value) for value in self._source_heat_kw.values()) * dt_h
        self.totals.pv_kwh += conditions.solar_pv_kw * dt_h
        self.totals.import_kwh += imported * dt_h
        self.totals.export_kwh += exported * dt_h
        self.totals.cost += (imported * conditions.electricity_price_kwh + gas_kw * conditions.gas_price_kwh) * dt_h
        self._balance = {
            "room_air_net_kw": sum(air_flow.values()), "fabric_net_kw": sum(fabric_flow.values()),
            "water_net_kw": sum(water_flow.values()), "slab_net_kw": sum(slab_heat.values()),
            "electricity_kw": electricity_kw, "gas_kw": gas_kw, "net_source_heat_kw": heat_kw,
            "solar_collector_kw": solar_collected, "pv_kw": conditions.solar_pv_kw,
            "import_kw": imported, "export_kw": exported,
        }

    def _source_loop(self, name, source):
        if not source.dhw_loop:
            return source.loop
        tank = self.hydronics.loops[source.dhw_loop]
        temperature = self.hydronics.water[source.dhw_loop]
        was_charging = self._dhw_charging.get(name, False)
        charging = temperature < tank.target_c - (0.05 if was_charging else 3)
        self._dhw_charging[name] = charging
        return source.dhw_loop if charging else source.loop

    def _outdoor_allocation(self, requests, available, outdoor):
        allocated = {}
        overhead = 0.0
        for name, unit in self.outdoor_units.items():
            demand = {
                key: requests[key] * source.max_heat_kw
                for key, source in self.sources.items()
                if source.outdoor_unit == name and key in available
            }
            total = sum(demand.values())
            direction = 1 if total >= 0 else -1
            if unit.mode_rule == "heat_priority" and any(value > 0 for value in demand.values()):
                direction = 1
            elif unit.mode_rule == "cool_priority" and any(value < 0 for value in demand.values()):
                direction = -1
            same_mode = {key: abs(value) for key, value in demand.items() if value * direction > 0}
            capacity = max(0, unit.capacity_kw + unit.capacity_per_outdoor_k * (outdoor - unit.reference_c))
            defrost = bool(
                same_mode and direction > 0 and outdoor <= unit.defrost_below_c
                and unit.defrost_period_s and unit.defrost_duration_s
                and self.elapsed_s % unit.defrost_period_s >= unit.defrost_period_s - unit.defrost_duration_s
            )
            electrical_kw = unit.defrost_power_kw if defrost else 0.0
            overhead += electrical_kw
            factor = min(1, capacity / sum(same_mode.values())) if same_mode and not defrost else 0
            if unit.allocation == "in_order" and not defrost:
                remaining = capacity
                share = {}
                for key, value in same_mode.items():
                    share[key] = min(remaining, value)
                    remaining -= share[key]
            else:
                share = {key: value * factor for key, value in same_mode.items()}
            allocated.update({key: direction * value for key, value in share.items()})
            self._outdoor_details[name] = {
                "mode": ("heating" if direction > 0 else "cooling") if same_mode else "idle",
                "capacity_kw": capacity, "requested_kw": sum(abs(value) for value in demand.values()),
                "allocated_kw": sum(share.values()), "defrost": defrost,
                "electrical_kw": electrical_kw,
            }
        return allocated, overhead

    def _source_output(self, source, requested_kw, limit_kw, available):
        state = self._source_state[source.name]
        since_change = self.elapsed_s - state.changed_at_s
        output = requested_kw
        status = "running" if output else "idle"
        if not available:
            output, status = 0.0, "unavailable"
        elif limit_kw <= 0:
            output, status = 0.0, "capacity limit"
        elif state.on and not requested_kw and since_change < source.min_on_s:
            output = min(abs(state.output_kw), limit_kw) * (1 if state.output_kw >= 0 else -1)
            status = "minimum run time"
        elif not state.on and requested_kw and since_change < source.min_off_s:
            output, status = 0.0, "minimum rest time"
        on = output != 0
        if on != state.on:
            state.on = on
            state.changed_at_s = self.elapsed_s
            state.starts += int(on)
        state.output_kw = output
        return output, status

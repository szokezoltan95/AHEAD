"""Thermal network for rooms, fabric, shared hydronics, and heat sources.

Heat flows are in kW and thermal capacities are in kWh/K. Each heat transfer
between simulated nodes enters one node and leaves the other in the same step.
"""

from dataclasses import dataclass, field
from math import isfinite
from typing import Mapping

from ahead.control import ControlPlan, Equipment, SourceCapabilities
from ahead.simulator.moisture import AIR_KWH_KG_K, LATENT_KWH_KG, humidity_ratio, relative_humidity


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
    initial_humidity_pct: float = 50


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
    indoor_fan_kw: float = 0.045
    report_indoor_humidity: bool = False
    humidity_setpoint_control: bool = False
    automatic_heat_calls: bool = True
    water_hysteresis_c: float = 0
    heat_pump_tariff: bool = False
    pump_start_c: float = 35.0
    pump_stop_c: float = 30.0
    pump_overrun_s: float = 180.0
    pump_kw: float = 0.06


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
    outdoor_humidity_pct: float = 75
    solar_by_zone_kw: Mapping[str, float] = field(default_factory=dict)
    ventilation_kw_k: Mapping[str, float] = field(default_factory=dict)
    door_exchange_kw_k: Mapping[tuple[str, str], float] = field(default_factory=dict)
    radiant_opening_m2: Mapping[tuple[str, str], float] = field(default_factory=dict)
    solar_water_kw: Mapping[str, float] = field(default_factory=dict)
    solar_thermal: Mapping[str, tuple[str, float, float]] = field(default_factory=dict)
    solar_pv_kw: float = 0
    dhw_draw_l_min: Mapping[str, float] = field(default_factory=dict)
    heat_pump_price_kwh: float | None = None


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
    house_electricity_kwh: float = 0
    heat_pump_electricity_kwh: float = 0
    heat_pump_cost: float = 0


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
        pipe_network=None,
    ):
        self.zones = {zone.name: zone for zone in zones}
        self.sources = {source.name: source for source in sources}
        if not zones or len(self.zones) != len(zones) or len(self.sources) != len(sources):
            raise ValueError("A house needs uniquely named zones and sources")
        self._partitions = partitions
        self._air = {zone.name: zone.initial_c for zone in zones}
        self._fabric = self._air.copy()
        self._moisture = {
            zone.name: humidity_ratio(zone.initial_c, zone.initial_humidity_pct) for zone in zones
        }
        loops = dict(loops or ({"main": loop} if loop else {}))
        emitters = dict(emitters) if emitters is not None else {
            zone.name: Emitter(zone.name, "main", zone.radiator_kw_k, zone.radiator_flow_l_min)
            for zone in zones if zone.radiator_kw_k
        }
        self.outdoor_units = dict(outdoor_units or {})
        self.exchangers = dict(exchangers or {})
        self._exchanger_details = {}
        self._room_flux = {}
        self._heat_links = []
        self._source_details = {}
        self._outdoor_details = {}
        self._balance = {}
        self._dhw_charging: dict[str, bool] = {}
        self._source_state = {name: _SourceState() for name in self.sources}
        self._pump_overrun = {name: 0.0 for name, source in self.sources.items()
                              if source.direct_zone is None and source.pump_kw > 0}
        self._pump_enabled = {self.sources[name].loop: False for name in self._pump_overrun}
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
            if not 0 <= zone.initial_humidity_pct <= 100:
                raise ValueError("Room humidity must be between zero and 100")
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
            if not isfinite(emitter.local_sensor_bias_c) or not 0 <= emitter.local_sensor_warming_c <= 20:
                raise ValueError("Emitter sensor calibration is invalid")
            air_conductance[emitter.zone] += emitter.slab_to_room_kw_k or emitter.kw_k
        self.hydronics = Hydronics(loops, emitters, self._air, pipe_network)
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
        self._outdoor_tariffs: dict[str, bool] = {}
        for source in sources:
            _check((source.max_heat_kw, source.efficiency), positive=True)
            _check((source.min_on_s, source.min_off_s, source.min_fraction, source.indoor_fan_kw))
            _check((source.water_hysteresis_c,))
            if source.water_hysteresis_c and (source.modulating or source.direct_zone is not None):
                raise ValueError("Water hysteresis requires an on/off hydronic source")
            if source.heat_pump_tariff and source.fuel != "electricity":
                raise ValueError("The heat-pump electricity meter requires an electric source")
            if source.outdoor_unit:
                previous = self._outdoor_tariffs.setdefault(source.outdoor_unit, source.heat_pump_tariff)
                if previous != source.heat_pump_tariff:
                    raise ValueError("Indoor units sharing a compressor must use the same meter")
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
            frozenset(name for name, emitter in emitters.items() if not emitter.fixed_open),
            {name: emitter.zone for name, emitter in emitters.items() if not emitter.fixed_open},
        )

        # Bound each explicit integration step by the fastest thermal node.
        max_rate = max(
            max(air_conductance[z.name] / z.air_capacity,
                (z.air_fabric_kw_k + z.fabric_outside_kw_k + z.ground_kw_k) / z.fabric_capacity)
            for z in zones
        )
        if pipe_network:
            max_rate += sum(
                pipe.kw_k / (self.zones[pipe.room].fabric_capacity
                             if pipe.mounting in ("in_wall", "underfloor")
                             else self.zones[pipe.room].air_capacity)
                for pipe in pipe_network.pipes.values() if pipe.room
            )
        self._max_rate = max((max_rate, *self.hydronics.rates())) + max(exchanger_rates.values(), default=0)

    def reset_run_clock(self) -> None:
        """Preserve thermal state while starting a new measurement window."""
        self.elapsed_s = 0.0
        self.totals = Totals()
        self._source_state = {name: _SourceState() for name in self.sources}
        self._pump_overrun = {name: 0.0 for name, source in self.sources.items()
                              if source.direct_zone is None and source.pump_kw > 0}
        self._source_heat_kw = dict.fromkeys(self.sources, 0.0)
        self.hydronics.valves = {name: float(emitter.fixed_open)
                                 for name, emitter in self.hydronics.emitters.items()}
        self._source_details.clear()
        self._outdoor_details.clear()
        self._balance.clear()
        self._dhw_charging.clear()

    def checkpoint(self) -> dict:
        return {
            "air_c": self._air.copy(), "fabric_c": self._fabric.copy(),
            "water_c": self.hydronics.water.copy(), "slab_c": self.hydronics.slab.copy(),
            "moisture": self._moisture.copy(),
        }

    def restore_checkpoint(self, snapshot: dict) -> None:
        for key, target in (("air_c", self._air), ("fabric_c", self._fabric),
                            ("water_c", self.hydronics.water), ("slab_c", self.hydronics.slab),
                            ("moisture", self._moisture)):
            if key == "moisture" and key not in snapshot:
                target.update({name: humidity_ratio(self._air[name], zone.initial_humidity_pct)
                               for name, zone in self.zones.items()})
                continue
            values = snapshot[key]
            if target.keys() != values.keys() or not all(map(isfinite, values.values())):
                raise ValueError("Checkpoint does not match this house")
            target.update(values)
        self.reset_run_clock()

    @property
    def indoor_c(self) -> dict[str, float]:
        return self._air.copy()

    @property
    def indoor_humidity(self) -> dict[str, float]:
        return {name: relative_humidity(self._air[name], ratio) for name, ratio in self._moisture.items()}

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
                       "humidity_pct": relative_humidity(self._air[name], self._moisture[name]),
                       **self._room_flux.get(name, {})}
                for name in self.zones
            },
            "circuits": {
                name: {**self.hydronics.circuits.get(name, {}), "supply_c": temperature,
                       "target_c": self.hydronics.loops[name].target_c}
                for name, temperature in self.hydronics.water.items()
            },
            "emitters": self.hydronics.details,
            "pipes": self.hydronics.pipe_network.details if self.hydronics.pipe_network else {},
            "sources": self._source_details,
            "outdoor_units": self._outdoor_details,
            "heat_exchangers": self._exchanger_details,
            "balance": self._balance,
            "heat_links": self._heat_links,
        }

    @property
    def source_starts(self) -> dict[str, int]:
        return {name: state.starts for name, state in self._source_state.items()}

    @property
    def source_details(self) -> Mapping[str, dict]:
        return self._source_details

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
                *conditions.door_exchange_kw_k.values(), *conditions.radiant_opening_m2.values(),
                *conditions.dhw_draw_l_min.values()))
        if conditions.heat_pump_price_kwh is not None:
            _check((conditions.heat_pump_price_kwh,))
        if not all(map(isfinite, (conditions.outdoor_c, conditions.electricity_price_kwh,
                                  conditions.gas_price_kwh))):
            raise ValueError("Weather and tariffs must be finite")
        if not 0 <= conditions.outdoor_humidity_pct <= 100:
            raise ValueError("Outdoor humidity must be between zero and 100")
        for name, fraction in plan.indoor_fans.items():
            if name not in self.sources or self.sources[name].direct_zone is None or not 0 <= fraction <= 1:
                raise ValueError("Invalid indoor fan command")
        if plan.sources.keys() - self.sources.keys() or plan.valves.keys() - self.equipment.valves:
            raise ValueError("Control plan references an unknown actuator")
        for fraction in plan.valves.values():
            if not 0 <= fraction <= 1:
                raise ValueError("Valve commands must be between zero and one")
        for name, fraction in plan.sources.items():
            source = self.sources[name]
            if not (-1 if source.reversible else 0) <= fraction <= 1:
                raise ValueError(f"Invalid request for {name}")
            if not source.modulating and fraction not in (-1, 0, 1):
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
        radiant_rates = dict.fromkeys(self.zones, 0.0)
        for (first, second), area in conditions.radiant_opening_m2.items():
            maximum_k = max(1, self._fabric[first]+273.15, self._fabric[second]+273.15)
            conductance = 4 * 5.670374419e-11 * area * maximum_k**3
            radiant_rates[first] += conductance / self.zones[first].fabric_capacity
            radiant_rates[second] += conductance / self.zones[second].fabric_capacity
        max_rate += max(radiant_rates.values(), default=0)
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
        self.hydronics.exchange(self._air, plan.valves, 0, fabric=self._fabric,
                                outdoor=conditions.outdoor_c, ground=conditions.ground_c,
                                pump_enabled=self._pump_enabled)

    def _step(self, dt_h, conditions, plan, available_sources):
        air_flow = dict.fromkeys(self.zones, 0.0)
        fabric_flow = dict.fromkeys(self.zones, 0.0)
        moisture_flow = dict.fromkeys(self.zones, 0.0)
        dry_air_kg = {name: zone.air_capacity / AIR_KWH_KG_K for name, zone in self.zones.items()}
        outdoor_moisture = humidity_ratio(conditions.outdoor_c, conditions.outdoor_humidity_pct)
        pump_enabled = {self.sources[name].loop: False for name in self._pump_overrun}
        for name, remaining in self._pump_overrun.items():
            source = self.sources[name]
            water = self.hydronics.water[source.loop]
            if self._source_state[name].on:
                self._pump_overrun[name] = source.pump_overrun_s
                pump_enabled[source.loop] = True
            elif remaining > 0 and water > source.pump_stop_c:
                self._pump_overrun[name] = max(0, remaining - dt_h * 3600)
                pump_enabled[source.loop] = self._pump_overrun[name] > 0
            elif water >= source.pump_start_c:
                pump_enabled[source.loop] = True
            else:
                self._pump_overrun[name] = 0
                pump_enabled[source.loop] = False
        self._pump_enabled = pump_enabled
        water_flow, emitter_heat, slab_heat = self.hydronics.exchange(
            self._air, plan.valves, dt_h * 3600, fabric=self._fabric,
            outdoor=conditions.outdoor_c, ground=conditions.ground_c,
            pump_enabled=pump_enabled
        )
        electricity_kw = conditions.appliance_kw
        heat_pump_kw = 0.0
        pump_kw = sum(self.sources[name].pump_kw for name in self._pump_overrun
                      if pump_enabled.get(self.sources[name].loop, False))
        electricity_kw += pump_kw
        gas_kw = heat_kw = 0.0
        outdoor = conditions.outdoor_c
        appliance_gains = (conditions.occupied_gains_kw + conditions.appliance_kw) / len(self.zones)

        for name, zone in self.zones.items():
            air = self._air[name]
            fabric = self._fabric[name]
            coupling = (fabric - air) * zone.air_fabric_kw_k
            air_exchange = zone.infiltration_kw_k + conditions.ventilation_kw_k.get(name, 0)
            ventilation = (outdoor - air) * air_exchange
            moisture_flow[name] = (outdoor_moisture - self._moisture[name]) * air_exchange / AIR_KWH_KG_K
            moisture_flow[name] += conditions.occupied_gains_kw * 0.6 / len(self.zones)
            shell = (outdoor - fabric) * zone.fabric_outside_kw_k
            ground = (conditions.ground_c - fabric) * zone.ground_kw_k
            gains = zone.base_gains_kw + appliance_gains
            sun = conditions.solar_kw_m2 * zone.solar_area_m2 + conditions.solar_by_zone_kw.get(name, 0)
            pipe_air = self.hydronics.pipe_air_heat[name]
            pipe_fabric = self.hydronics.pipe_fabric_heat[name]
            air_flow[name] = coupling + ventilation + gains + emitter_heat[name] + pipe_air
            fabric_flow[name] = -coupling + shell + ground + sun + pipe_fabric
            self._room_flux[name] = {
                "fabric_to_air_kw": coupling, "ventilation_kw": ventilation, "envelope_kw": shell,
                "ground_kw": ground, "internal_gains_kw": gains, "solar_kw": sun, "emitters_kw": emitter_heat[name],
                "neighbours_kw": 0.0, "direct_heat_kw": 0.0,
                "pipe_to_air_kw": pipe_air, "pipe_to_fabric_kw": pipe_fabric,
            }

        heat_links = []

        def transfer(first, second, mechanism, power):
            # Positive power transfers energy from the first room to the second.
            node = fabric_flow if mechanism == "radiation" else air_flow
            node[first] -= power
            node[second] += power
            field = "radiation_kw" if mechanism == "radiation" else "neighbours_kw"
            for room, amount in ((first, -power), (second, power)):
                self._room_flux[room][field] = self._room_flux[room].get(field, 0) + amount
            heat_links.append({"first": first, "second": second, "kind": mechanism, "kw": power})

        for link in self._partitions:
            transfer(link.first, link.second, "conduction",
                     (self._air[link.first] - self._air[link.second]) * link.kw_k)
        for (first, second), conductance in conditions.door_exchange_kw_k.items():
            transfer(first, second, "airflow", (self._air[first] - self._air[second]) * conductance)
            vapour = (self._moisture[first] - self._moisture[second]) * conductance / AIR_KWH_KG_K
            moisture_flow[first] -= vapour
            moisture_flow[second] += vapour
        for (first, second), area in conditions.radiant_opening_m2.items():
            first_k = max(0, self._fabric[first] + 273.15)
            second_k = max(0, self._fabric[second] + 273.15)
            power = 5.670374419e-11 * area * (first_k**4 - second_k**4)
            transfer(first, second, "radiation", power)
        self._heat_links = heat_links

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
        allocated = self._outdoor_allocation(demand, available_sources, outdoor)
        for name, detail in self._outdoor_details.items():
            if self._outdoor_tariffs.get(name, False):
                heat_pump_kw += detail["electrical_kw"]
            else:
                electricity_kw += detail["electrical_kw"]
        for name, source in self.sources.items():
            request = demand[name]
            loop_name = self._source_loop(name, source)
            water_target = None
            stopped = name in plan.stopped_sources
            limit_kw = source.max_heat_kw
            if source.direct_zone is None:
                loop = self.hydronics.loops[loop_name]
                water_target = loop.target_c
                if loop_name == source.loop:
                    water_target = min(loop.target_c, plan.water_setpoints_c.get(name, loop.target_c))
                headroom = (water_target - self.hydronics.water[loop_name]) * loop.capacity / dt_h
                limit_kw = min(limit_kw, max(0.0, headroom - water_flow[loop_name]))
                if source.water_hysteresis_c:
                    threshold = water_target
                    if not self._source_state[name].on:
                        threshold -= source.water_hysteresis_c
                    stopped = (stopped or not request or self.hydronics.water[loop_name] >= threshold
                               or limit_kw < source.max_heat_kw)
            if source.outdoor_unit:
                requested_kw = allocated.get(name, 0.0)
                limit_kw = abs(requested_kw)
            elif request < 0:
                requested_kw = -min(abs(request) * source.max_heat_kw, limit_kw)
            elif request:
                requested_kw = min(max(request, source.min_fraction) * source.max_heat_kw, limit_kw)
            else:
                requested_kw = 0.0

            delivered, status = self._source_output(source, requested_kw, limit_kw,
                name in available_sources, stopped)
            self._source_heat_kw[name] = delivered
            latent_kw = 0.0
            fan_kw = 0.0
            sensible = delivered
            if source.direct_zone is None:
                water_flow[loop_name] += delivered
            else:
                zone = source.direct_zone
                fan = plan.indoor_fans.get(name, 0)
                if fan and name in available_sources and (delivered or name in plan.fan_only_sources):
                    fan_kw = source.indoor_fan_kw * fan**3
                    if delivered < 0:
                        coil_moisture = humidity_ratio(max(5, self._air[zone] - 12), 100)
                        available_water = max(0, self._moisture[zone] - coil_moisture) * dry_air_kg[zone]
                        fraction = 0.5 if name in plan.dry_sources else 0.25
                        latent_kw = min(-delivered * fraction, available_water * LATENT_KWH_KG / dt_h)
                        moisture_flow[zone] -= latent_kw / LATENT_KWH_KG
                sensible = delivered + latent_kw + fan_kw
                air_flow[zone] += sensible
                self._room_flux[zone]["direct_heat_kw"] += sensible
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
            elif source.heat_pump_tariff:
                heat_pump_kw += power + fan_kw
            else:
                electricity_kw += power + fan_kw
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
                "input_kw": power + fan_kw, "fuel": source.fuel, "efficiency": efficiency, "status": status,
                "fan_kw": fan_kw, "latent_cooling_kw": latent_kw, "sensible_heat_kw": sensible,
                "starts": self._source_state[name].starts,
                "heat_pump_tariff": source.heat_pump_tariff,
                "pump_on": bool(source.direct_zone is None and pump_enabled.get(loop_name, False)),
                "pump_kw": source.pump_kw if source.direct_zone is None and pump_enabled.get(loop_name, False) else 0,
            }

        for name, zone in self.zones.items():
            before = self._air[name] - zone.setpoint_c
            self._air[name] += air_flow[name] * dt_h / zone.air_capacity
            self._fabric[name] += fabric_flow[name] * dt_h / zone.fabric_capacity
            humidity = self._moisture[name] + moisture_flow[name] * dt_h / dry_air_kg[name]
            self._moisture[name] = min(humidity_ratio(self._air[name],100), max(0,humidity))
            after = self._air[name] - zone.setpoint_c
            area = (abs(before) + abs(after)) * dt_h / 2
            if before * after < 0:
                area *= (before**2 + after**2) / (after - before)**2
            if zone.conditioned:
                self.totals.comfort_degree_hours += area
        self.hydronics.advance(dt_h, water_flow, slab_heat)
        imported = max(0, electricity_kw - conditions.solar_pv_kw)
        exported = max(0, conditions.solar_pv_kw - electricity_kw)
        self.totals.house_electricity_kwh += electricity_kw * dt_h
        self.totals.heat_pump_electricity_kwh += heat_pump_kw * dt_h
        self.totals.electricity_kwh += (electricity_kw + heat_pump_kw) * dt_h
        self.totals.gas_kwh += gas_kw * dt_h
        self.totals.generated_heat_kwh += sum(max(0, value) for value in self._source_heat_kw.values()) * dt_h
        self.totals.cooling_kwh += sum(max(0, -value) for value in self._source_heat_kw.values()) * dt_h
        self.totals.pv_kwh += conditions.solar_pv_kw * dt_h
        self.totals.import_kwh += (imported + heat_pump_kw) * dt_h
        self.totals.export_kwh += exported * dt_h
        heat_pump_rate = conditions.heat_pump_price_kwh
        if heat_pump_rate is None:
            heat_pump_rate = conditions.electricity_price_kwh
        heat_pump_cost = heat_pump_kw * heat_pump_rate * dt_h
        self.totals.heat_pump_cost += heat_pump_cost
        self.totals.cost += (imported * conditions.electricity_price_kwh + gas_kw * conditions.gas_price_kwh) * dt_h + heat_pump_cost
        self._balance = {
            "room_air_net_kw": sum(air_flow.values()), "fabric_net_kw": sum(fabric_flow.values()),
            "water_net_kw": sum(water_flow.values()), "slab_net_kw": sum(slab_heat.values()),
            "electricity_kw": electricity_kw + heat_pump_kw, "gas_kw": gas_kw, "net_source_heat_kw": heat_kw,
            "house_electricity_kw": electricity_kw, "heat_pump_electricity_kw": heat_pump_kw,
            "house_grid_import_kw": imported,
            "solar_collector_kw": solar_collected, "pv_kw": conditions.solar_pv_kw,
            "import_kw": imported + heat_pump_kw, "export_kw": exported,
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
        return allocated

    def _source_output(self, source, requested_kw, limit_kw, available, stopped=False):
        state = self._source_state[source.name]
        since_change = self.elapsed_s - state.changed_at_s
        output = requested_kw
        status = "running" if output else "idle"
        if not available:
            output, status = 0.0, "unavailable"
        elif stopped:
            output, status = 0.0, "idle"
        elif limit_kw <= 0:
            output, status = 0.0, "capacity limit"
        elif state.on and state.output_kw * requested_kw < 0 and source.min_off_s:
            output, status = 0.0, "mode change lockout"
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

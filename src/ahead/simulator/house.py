"""Thermal network for rooms, fabric, shared hydronics, and heat sources.

Heat flows are in kW and thermal capacities are in kWh/K. Each heat transfer
between simulated nodes enters one node and leaves the other in the same step.
"""

from dataclasses import dataclass, field, replace
from math import atan2, ceil, cos, exp, expm1, isfinite, pi, radians, sin, sqrt
from typing import Mapping

from ahead.control import ControlPlan, Equipment, SourceCapabilities
from ahead.simulator.moisture import AIR_KWH_KG_K, LATENT_KWH_KG, humidity_ratio, relative_humidity


from ahead.simulator.hydronics import Emitter, HeatExchanger, Hydronics, WaterLoop, WATER_KW_PER_K_PER_L_MIN


AIR_CELL_SIZE_M = 0.5
# Effective turbulent exchange between half-metre room-air cells. This is a
# room-scale mixing coefficient, not molecular conduction through still air.
AIR_CELL_MIX_KW_K = 0.018
AIR_CELL_BUOYANCY_KW_K = 0.003
AIR_CELL_FAN_MIX_KW_K = 0.008
AIR_CELL_JET_KW_K = 0.04
# Radiator plumes entrain surrounding air as they rise. At room scale their
# effective temperature excess is much lower than the radiator-face excess;
# using 2 K gives the corresponding larger recirculating air mass flow.
AIR_RADIATOR_PLUME_DELTA_C = 2.0
AIR_EMITTER_SOURCE_SIGMA_M = 0.35
AIR_AC_SOURCE_SIGMA_M = 0.45
AIR_DOOR_INTERACTION_DISTANCE_M = 0.5
AIR_DOOR_CLOSED_KW_K_M2 = 0.002
AIR_DOOR_OPEN_KW_K_M2 = 0.2


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
    contents_capacity_kwh_k: float = 0
    air_contents_kw_k: float = 0
    fabric_core_capacity_kwh_k: float = 0
    fabric_core_kw_k: float = 0
    fabric_surface_outside_kw_k: float = 0


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
    boiler_water_volume_l: float = 0.0
    boiler_metal_mass_kg: float = 0.0
    boiler_metal_cp_j_kgk: float = 500.0
    boiler_flow_l_min: float = 0.0
    boiler_water_ua_kw_k: float = 0.0
    boiler_loss_kw_k: float = 0.0
    boiler_room_zone: str | None = None
    vertical_swing_available: bool = True
    horizontal_swing_available: bool = True
    vertical_angle_deg: float = 30.0
    horizontal_angle_deg: float = 0.0
    vertical_swing_amplitude_deg: float = 25.0
    horizontal_swing_amplitude_deg: float = 45.0
    vertical_swing_period_s: float = 8.0
    horizontal_swing_period_s: float = 12.0


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
        air_source_positions: Mapping[str, tuple[float, float, float]] | None = None,
        air_source_facing_deg: Mapping[str, float] | None = None,
        room_dimensions_m: Mapping[str, tuple[float, float, float]] | None = None,
        air_openings: tuple[dict, ...] = (),
    ):
        self.zones = {zone.name: zone for zone in zones}
        self.sources = {source.name: source for source in sources}
        if not zones or len(self.zones) != len(zones) or len(self.sources) != len(sources):
            raise ValueError("A house needs uniquely named zones and sources")
        self._partitions = partitions
        self._air = {zone.name: zone.initial_c for zone in zones}
        self._air_grid_shape = {}
        self._air_cell_size_m = {}
        self._air_cells = {}
        dimensions = dict(room_dimensions_m or {})
        for zone in zones:
            dims = dimensions.get(zone.name)
            if dims is None:
                volume_m3 = zone.air_capacity * 3600 / (1.2 * 1.005)
                side = volume_m3 ** (1 / 3)
                dims = (side, side, side)
            if len(dims) != 3 or not all(isfinite(value) and value > 0 for value in dims):
                raise ValueError("Room air-grid dimensions must be positive finite lengths")
            nx, ny, nz = (max(1, ceil(length / AIR_CELL_SIZE_M)) for length in dims)
            dx, dy, dz = (length / count for length, count in zip(dims, (nx, ny, nz)))
            self._air_grid_shape[zone.name] = (nx, ny, nz)
            self._air_cell_size_m[zone.name] = (dx, dy, dz)
            self._air_cells[zone.name] = [
                [[zone.initial_c for _ in range(nx)] for _ in range(ny)]
                for _ in range(nz)
            ]

        self._fabric = self._air.copy()
        self._fabric_core = self._air.copy()
        self._contents = self._air.copy()
        self._moisture = {
            zone.name: humidity_ratio(zone.initial_c, zone.initial_humidity_pct) for zone in zones
        }
        self._air_opening_edges = []
        self._air_opening_conductance = []
        edge_cell_load = {}
        for opening in air_openings:
            first, second = opening["first_room"], opening["second_room"]
            cells = [self._opening_cells(opening, "first"), self._opening_cells(opening, "second")]
            if not cells[0] or not cells[1]:
                continue
            area = opening["width_m"] * opening["height_m"]
            fraction = opening["openness"] if opening["kind"] == "door" else 1.0
            total_k = area * (AIR_DOOR_CLOSED_KW_K_M2 +
                              (AIR_DOOR_OPEN_KW_K_M2 - AIR_DOOR_CLOSED_KW_K_M2) * fraction)
            pairs = []
            remaining = set(range(len(cells[1])))
            for cell_a in cells[0]:
                if not remaining:
                    break
                first_height = cell_a[3] + opening.get("first_sill_m", 0)
                index = min(remaining, key=lambda i: (cells[1][i][3] + opening.get("second_sill_m", 0) - first_height) ** 2 +
                            (cells[1][i][4] - cell_a[4]) ** 2)
                remaining.remove(index)
                pairs.append((cell_a[0], cells[1][index][0]))
            conductance = total_k / len(pairs)
            self._air_opening_conductance.append((first, second, total_k))
            for cell_a, cell_b in pairs:
                self._air_opening_edges.append((first, cell_a, second, cell_b, conductance))
                edge_cell_load[(first, cell_a)] = edge_cell_load.get((first, cell_a), 0) + conductance
                edge_cell_load[(second, cell_b)] = edge_cell_load.get((second, cell_b), 0) + conductance
        loops = dict(loops or ({"main": loop} if loop else {}))
        # Boiler water and heat-exchanger metal are part of the circulating
        # circuit's thermal mass, not a separate tank coupled through a UA.
        boiler_capacity_by_loop = dict.fromkeys(loops, 0.0)
        for source in self.sources.values():
            if source.boiler_water_volume_l > 0 and source.direct_zone is None:
                boiler_capacity_by_loop[source.loop] += (
                    source.boiler_water_volume_l * 4.186 / 3600 +
                    source.boiler_metal_mass_kg * source.boiler_metal_cp_j_kgk / 3_600_000
                )
        loops = {
            name: replace(item, capacity=item.capacity + boiler_capacity_by_loop[name])
            for name, item in loops.items()
        }
        emitters = dict(emitters) if emitters is not None else {
            zone.name: Emitter(zone.name, "main", zone.radiator_kw_k, zone.radiator_flow_l_min)
            for zone in zones if zone.radiator_kw_k
        }
        positions = dict(air_source_positions or {})
        self._air_source_weights = {}
        self._air_source_positions = {}
        self._air_source_facing_deg = dict(air_source_facing_deg or {})
        for name, emitter in emitters.items():
            position = positions.get(name, (.5, .82, .18))
            if len(position) == 2:
                position = (*position, .18)
            self._air_source_positions[name] = position
            self._air_source_weights[name] = (
                self._emitter_air_weights(emitter.zone, *position, emitter.width_m, emitter.height_m)
                if emitter.rated_output_kw > 0 else
                self._point_weights(emitter.zone, *position, sigma=AIR_EMITTER_SOURCE_SIGMA_M)
            )
        for name, source in self.sources.items():
            if source.direct_zone is not None:
                default = (.5, .5, .85) if source.outdoor_unit else (.5, .5, .18)
                position = positions.get(name, default)
                if len(position) == 2:
                    position = (*position, default[2])
                self._air_source_positions[name] = position
                self._air_source_weights[name] = self._point_weights(
                    source.direct_zone, *position,
                    sigma=AIR_AC_SOURCE_SIGMA_M if source.outdoor_unit else AIR_EMITTER_SOURCE_SIGMA_M)
        # Precompute the room-wide radiator circulation from saved room geometry
        # and emitter positions. Runtime steps scale it by convective output.
        self._air_circulation_patterns = {
            name: self._build_radiator_circulation_pattern(emitter.zone, name, emitter.width_m)
            for name, emitter in emitters.items()
        }
        self._air_currents = {name: self._sample_air_currents(name, ()) for name in self.zones}
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
        self.boiler_hold_60 = {}
        self._boiler_water_c = {
            name: loops[source.loop].initial_c
            for name, source in self.sources.items() if source.boiler_water_volume_l > 0
        }
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
                    zone.base_gains_kw, zone.ground_kw_k, zone.contents_capacity_kwh_k,
                    zone.air_contents_kw_k, zone.fabric_core_capacity_kwh_k,
                    zone.fabric_core_kw_k, zone.fabric_surface_outside_kw_k))
            if zone.air_contents_kw_k and not zone.contents_capacity_kwh_k:
                raise ValueError("Room contents need positive thermal capacity")
            if zone.radiator_kw_k and not zone.radiator_flow_l_min:
                raise ValueError("Radiators need a positive design flow")
            if not 0 <= zone.initial_humidity_pct <= 100:
                raise ValueError("Room humidity must be between zero and 100")
            if not all(map(isfinite, (zone.initial_c, zone.setpoint_c))):
                raise ValueError("Zone temperatures must be finite")
            air_conductance[zone.name] = (
                zone.air_fabric_kw_k + zone.air_contents_kw_k + zone.infiltration_kw_k
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
            _check((source.min_on_s, source.min_off_s, source.min_fraction, source.indoor_fan_kw,
                    source.boiler_water_volume_l, source.boiler_metal_mass_kg,
                    source.boiler_metal_cp_j_kgk, source.boiler_flow_l_min,
                    source.boiler_water_ua_kw_k, source.boiler_loss_kw_k))
            if source.boiler_water_volume_l and source.boiler_metal_mass_kg and not source.boiler_metal_cp_j_kgk:
                raise ValueError("Boiler metal thermal mass needs a positive specific heat")
            _check((source.vertical_swing_period_s, source.horizontal_swing_period_s), positive=True)
            _check((source.vertical_angle_deg, source.horizontal_angle_deg,
                    source.vertical_swing_amplitude_deg, source.horizontal_swing_amplitude_deg))
            if not (-60 <= source.vertical_angle_deg <= 75 and -90 <= source.horizontal_angle_deg <= 90
                    and 0 <= source.vertical_swing_amplitude_deg <= 60
                    and 0 <= source.horizontal_swing_amplitude_deg <= 90):
                raise ValueError("A/C louver settings are out of range")
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
            if source.boiler_room_zone is not None and source.boiler_room_zone not in self.zones:
                raise ValueError("Boiler loss references an unknown room")
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
        max_rate = max(max(
            air_conductance[z.name] / z.air_capacity,
            ((z.air_fabric_kw_k + z.fabric_core_kw_k + z.fabric_surface_outside_kw_k)
             / z.fabric_capacity
             if z.fabric_core_capacity_kwh_k else
             (z.air_fabric_kw_k + z.fabric_outside_kw_k + z.ground_kw_k) / z.fabric_capacity),
            z.air_contents_kw_k / z.contents_capacity_kwh_k if z.contents_capacity_kwh_k else 0,
        ) for z in zones)
        for zone in zones:
            mix, buoyancy, _fan = self._grid_conductances(zone.name)
            cell_capacity = zone.air_capacity / self._cell_count(zone.name)
            # Turbulent inter-cell mixing is integrated with a stable pairwise
            # exchange below, so it does not constrain the house-wide step.
            grid_rate = buoyancy[2] / cell_capacity
            max_rate = max(max_rate, grid_rate)
            if zone.fabric_core_capacity_kwh_k:
                max_rate = max(max_rate,
                    (zone.air_fabric_kw_k + zone.fabric_core_kw_k + zone.fabric_surface_outside_kw_k)
                    / zone.fabric_capacity,
                    (zone.fabric_core_kw_k + zone.fabric_outside_kw_k + zone.ground_kw_k)
                    / zone.fabric_core_capacity_kwh_k)
        max_rate = max(max_rate, max((conductance / (self.zones[room].air_capacity /
            self._cell_count(room)) for (room, _cell), conductance in edge_cell_load.items()), default=0))
        if pipe_network:
            max_rate += sum(
                pipe.kw_k / (self.zones[pipe.room].fabric_capacity
                             if pipe.mounting in ("in_wall", "underfloor")
                             else self.zones[pipe.room].air_capacity)
                for pipe in pipe_network.pipes.values() if pipe.room
            )
            max_rate = max([max_rate, *pipe_network.rates()])
        self._max_rate = max([max_rate, *self.hydronics.rates()]) + max(exchanger_rates.values(), default=0)

    def _cell_count(self, room: str) -> int:
        nx, ny, nz = self._air_grid_shape[room]
        return nx * ny * nz

    def _opening_cells(self, opening: dict, prefix: str):
        room = opening[f"{prefix}_room"]
        side = opening[f"{prefix}_side"]
        offset = opening[f"{prefix}_offset_m"]
        nx, ny, nz = self._air_grid_shape[room]
        dx, dy, dz = self._air_cell_size_m[room]
        # Grid axes follow the room's plan length (x), width (y), then height (z).
        lengths = (nx * dx, ny * dy, nz * dz)
        along_x = side in ("north", "south")
        normal_length = lengths[1] if along_x else lengths[0]
        normal_size = dy if along_x else dx
        tangent_size = dx if along_x else dy
        tangent_count = nx if along_x else ny
        plane_side = {"north": "low", "west": "low", "south": "high", "east": "high"}[side]
        sill = opening.get(f"{prefix}_sill_m", 0)
        cells = []
        for iz in range(nz):
            z = (iz + .5) * dz
            if z < sill - dz / 2 or z > sill + opening["height_m"] + dz / 2:
                continue
            for tangent_index in range(tangent_count):
                tangent = (tangent_index + .5) * tangent_size
                if abs(tangent - offset) > opening["width_m"] / 2 + tangent_size / 2:
                    continue
                for normal_index in range((ny if along_x else nx)):
                    normal = (normal_index + .5) * normal_size
                    distance = normal if plane_side == "low" else normal_length - normal
                    if distance > AIR_DOOR_INTERACTION_DISTANCE_M + normal_size / 2:
                        continue
                    cell = ((iz, normal_index, tangent_index) if along_x
                            else (iz, tangent_index, normal_index))
                    cells.append((cell, distance, tangent, z, tangent - offset))
        return sorted(cells, key=lambda item: (item[4], item[3]))

    def _grid_conductances(self, room: str):
        dx, dy, dz = self._air_cell_size_m[room]
        areas = (dy * dz, dx * dz, dx * dy)
        lengths = (dx, dy, dz)
        scale = tuple(area / 0.25 * (0.5 / length) for area, length in zip(areas, lengths))
        return (
            tuple(AIR_CELL_MIX_KW_K * value for value in scale),
            tuple(AIR_CELL_BUOYANCY_KW_K * value for value in scale),
            tuple(AIR_CELL_FAN_MIX_KW_K * value for value in scale),
        )

    def _build_radiator_circulation_pattern(self, room: str, emitter_name: str, width_m: float):
        """Precompute conservative room-wide circulation faces for one radiator.

        The streamfunction forms a broad roll: warm air rises beside the
        radiator, returns across the ceiling, descends through the room, and
        moves back toward the radiator near the floor.
        """
        nx, ny, nz = self._air_grid_shape[room]
        if nz < 2 or min(nx, ny) < 2:
            return []

        sx, sy, _sz = self._air_source_positions.get(emitter_name, (.5, .82, .18))
        wall_axis = 0 if min(sx, 1 - sx) <= min(sy, 1 - sy) else 1
        if wall_axis == 0:
            wall_count, span_count = nx, ny
            wall_fraction, span_fraction = sx, sy
            span_length = self._air_cell_size_m[room][1] * ny
        else:
            wall_count, span_count = ny, nx
            wall_fraction, span_fraction = sy, sx
            span_length = self._air_cell_size_m[room][0] * nx
        wall_index = 0 if wall_fraction <= .5 else wall_count - 1
        wall_is_low = wall_index == 0

        width = max(.35, float(width_m or .8))
        sigma = max(width / 2.355, span_length / 6)
        span_dx = span_length / span_count
        center = span_fraction * span_length
        slice_weights = [
            exp(-(((index + .5) * span_dx - center) ** 2) / (2 * sigma**2))
            for index in range(span_count)
        ]
        total_weight = sum(slice_weights)
        slice_weights = [weight / total_weight for weight in slice_weights]

        edges = []
        direction = -1.0 if wall_is_low else 1.0
        for span_index, slice_weight in enumerate(slice_weights):
            streamfunction = [
                [direction * slice_weight * sin(pi * u / wall_count) * sin(pi * z / nz)
                 for z in range(nz + 1)]
                for u in range(wall_count + 1)
            ]

            def cell(u, z):
                if wall_axis == 0:
                    return (z, span_index, u)
                return (z, u, span_index)

            for u in range(1, wall_count):
                for z in range(nz):
                    flux = streamfunction[u][z + 1] - streamfunction[u][z]
                    upstream, downstream = ((cell(u - 1, z), cell(u, z)) if flux > 0
                                            else (cell(u, z), cell(u - 1, z)))
                    edges.append((upstream, downstream, abs(flux)))
            for u in range(wall_count):
                for z in range(1, nz):
                    flux = -(streamfunction[u + 1][z] - streamfunction[u][z])
                    upstream, downstream = ((cell(u, z - 1), cell(u, z)) if flux > 0
                                            else (cell(u, z), cell(u, z - 1)))
                    edges.append((upstream, downstream, abs(flux)))
        return edges

    def _point_weights(self, room: str, x: float, y: float, z: float, *, sigma: float):
        dx, dy, dz = self._air_cell_size_m[room]
        nx, ny, nz = self._air_grid_shape[room]
        dims = (nx * dx, ny * dy, nz * dz)
        point = tuple(max(0, min(1, fraction)) * length for fraction, length in zip((x, y, z), dims))
        weights = []
        total = 0.0
        for iz in range(nz):
            layer = []
            for iy in range(ny):
                row = []
                for ix in range(nx):
                    center = ((ix + .5) * dx, (iy + .5) * dy, (iz + .5) * dz)
                    distance2 = sum((value - target) ** 2 for value, target in zip(center, point))
                    value = exp(-distance2 / (2 * sigma**2))
                    row.append(value)
                    total += value
                layer.append(row)
            weights.append(layer)
        return [[[value / total for value in row] for row in layer] for layer in weights]

    def _emitter_air_weights(self, room: str, x: float, y: float, z: float,
                             width_m: float, height_m: float):
        """Distribute radiator convection over its wall-mounted footprint."""
        dx, dy, dz = self._air_cell_size_m[room]
        nx, ny, nz = self._air_grid_shape[room]
        length, breadth, room_height = nx * dx, ny * dy, nz * dz
        x = max(0.0, min(1.0, x)) * length
        y = max(0.0, min(1.0, y)) * breadth
        z = max(0.0, min(1.0, z)) * room_height
        # The radiator's long axis follows the closest wall. The wall-normal
        # coordinate selects the shallow air layer in front of its face.
        wall_axis = 0 if min(x, length - x) <= min(y, breadth - y) else 1
        if wall_axis == 0:
            tangent_center, tangent_size, tangent_count = y, dy, ny
            normal_center, normal_size, normal_count = x, dx, nx
            normal_length = length
        else:
            tangent_center, tangent_size, tangent_count = x, dx, nx
            normal_center, normal_size, normal_count = y, dy, ny
            normal_length = breadth
        wall_low = normal_center <= normal_length / 2
        wall = 0.0 if wall_low else normal_length
        tangent_lo = max(0.0, tangent_center - max(width_m, tangent_size) / 2)
        tangent_hi = min(tangent_count * tangent_size,
                         tangent_center + max(width_m, tangent_size) / 2)
        vertical_lo = max(0.0, z - max(height_m, dz) / 2)
        vertical_hi = min(room_height, z + max(height_m, dz) / 2)
        weights = [[[0.0 for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
        total = 0.0
        for iz in range(nz):
            z_lo, z_hi = iz * dz, (iz + 1) * dz
            vertical_overlap = max(0.0, min(z_hi, vertical_hi) - max(z_lo, vertical_lo))
            if not vertical_overlap:
                continue
            for iy in range(ny):
                for ix in range(nx):
                    tangent_index = iy if wall_axis == 0 else ix
                    t_lo, t_hi = tangent_index * tangent_size, (tangent_index + 1) * tangent_size
                    tangent_overlap = max(0.0, min(t_hi, tangent_hi) - max(t_lo, tangent_lo))
                    if not tangent_overlap:
                        continue
                    normal_index = ix if wall_axis == 0 else iy
                    normal_lo, normal_hi = normal_index * normal_size, (normal_index + 1) * normal_size
                    depth = (normal_lo + normal_hi) / 2 if wall_low else normal_length - (normal_lo + normal_hi) / 2
                    face_weight = exp(-depth * depth / (2 * AIR_EMITTER_SOURCE_SIGMA_M**2))
                    value = tangent_overlap * vertical_overlap * face_weight
                    weights[iz][iy][ix] = value
                    total += value
        if total <= 0:
            return self._point_weights(room, x / length, y / breadth, z / room_height,
                                       sigma=AIR_EMITTER_SOURCE_SIGMA_M)
        return [[[value / total for value in row] for row in layer] for layer in weights]

    def _airflow_vector(self, room: str, source: str, horizontal_deg: float, vertical_deg: float):
        dx, dy, dz = self._air_cell_size_m[room]
        nx, ny, nz = self._air_grid_shape[room]
        sx, sy, _ = self._air_source_positions[source]
        if source in self._air_source_facing_deg:
            # Floor-plan bearing is clockwise from north; the model's yaw is
            # measured counter-clockwise from the positive x axis.
            yaw = radians(self._air_source_facing_deg[source] - 90)
        else:
            forward_x, forward_y = nx * dx / 2 - sx * nx * dx, ny * dy / 2 - sy * ny * dy
            yaw = atan2(forward_y, forward_x)
        yaw += radians(horizontal_deg)
        pitch = radians(vertical_deg)
        return cos(pitch) * cos(yaw), cos(pitch) * sin(yaw), -sin(pitch)

    def _air_jet_profile(self, room: str, source: str, vector):
        dx, dy, dz = self._air_cell_size_m[room]
        nx, ny, nz = self._air_grid_shape[room]
        sx, sy, sz = self._air_source_positions[source]
        origin = (sx * nx * dx, sy * ny * dy, sz * nz * dz)
        profile = [[[0.0 for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
        for iz in range(nz):
            for iy in range(ny):
                for ix in range(nx):
                    relative = ((ix + .5) * dx - origin[0],
                                (iy + .5) * dy - origin[1],
                                (iz + .5) * dz - origin[2])
                    along = sum(a * b for a, b in zip(relative, vector))
                    if along < 0:
                        continue
                    distance2 = sum(value * value for value in relative) - along * along
                    radius = .18 + .12 * along
                    profile[iz][iy][ix] = exp(-distance2 / (2 * radius * radius)) * exp(-along / 6)
        return profile

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
            "fabric_core_c": self._fabric_core.copy(),
            "contents_c": self._contents.copy(),
            "water_c": self.hydronics.water.copy(), "slab_c": self.hydronics.slab.copy(),
            "radiator_c": self.hydronics.radiator_c.copy(),
            "boiler_water_c": {
                name: self.hydronics.water[self.sources[name].loop]
                for name in self._boiler_water_c
            },
            "moisture": self._moisture.copy(),
            "air_cells_c": {name: [[row.copy() for row in layer] for layer in grid]
                            for name, grid in self._air_cells.items()},
            "pipe_thermal": self.hydronics.pipe_network.checkpoint()
            if self.hydronics.pipe_network else None,
        }

    def restore_checkpoint(self, snapshot: dict) -> None:
        for key, target in (("air_c", self._air), ("fabric_c", self._fabric),
                            ("fabric_core_c", self._fabric_core),
                            ("contents_c", self._contents),
                            ("water_c", self.hydronics.water), ("slab_c", self.hydronics.slab),
                            ("radiator_c", self.hydronics.radiator_c),
                            ("moisture", self._moisture)):
            if key == "contents_c" and key not in snapshot:
                target.update(self._air)
                continue
            if key == "fabric_core_c" and key not in snapshot:
                target.update(snapshot.get("fabric_c", self._air))
                continue
            if key == "moisture" and key not in snapshot:
                target.update({name: humidity_ratio(self._air[name], zone.initial_humidity_pct)
                               for name, zone in self.zones.items()})
                continue
            if key == "radiator_c" and key not in snapshot:
                target.update({name: self._air[self.hydronics.emitters[name].zone] for name in target})
                continue
            values = snapshot[key]
            if target.keys() != values.keys() or not all(map(isfinite, values.values())):
                raise ValueError("Checkpoint does not match this house")
            target.update(values)
        cells = snapshot.get("air_cells_c")
        if cells is None:
            self._air_cells = {
                name: [[[self._air[name] for _ in range(self._air_grid_shape[name][0])]
                        for _ in range(self._air_grid_shape[name][1])]
                       for _ in range(self._air_grid_shape[name][2])]
                for name in self.zones
            }
        else:
            if cells.keys() != self.zones.keys():
                raise ValueError("Checkpoint air grid does not match this house")
            restored = {}
            for name, grid in cells.items():
                nx, ny, nz = self._air_grid_shape[name]
                try:
                    valid_shape = (len(grid) == nz and all(len(layer) == ny for layer in grid)
                                   and all(len(row) == nx for layer in grid for row in layer))
                except TypeError:
                    valid_shape = False
                if valid_shape and all(isfinite(value) for layer in grid for row in layer for value in row):
                    restored[name] = [[row.copy() for row in layer] for layer in grid]
                elif len(grid) == 7 and all(isinstance(row, list) and len(row) == 7 for row in grid):
                    # Upgrade checkpoints from the former 2D display grid.
                    restored[name] = [[[self._air[name] for _ in range(nx)] for _ in range(ny)]
                                      for _ in range(nz)]
                else:
                    raise ValueError("Checkpoint air grid does not match this house")
            self._air_cells = restored
            self._air = {name: sum(map(sum, (row for layer in grid for row in layer)))
                         / self._cell_count(name) for name, grid in self._air_cells.items()}
        if self.hydronics.pipe_network:
            if snapshot.get("pipe_thermal") is not None:
                self.hydronics.pipe_network.restore_checkpoint(snapshot["pipe_thermal"])
        if self._boiler_water_c:
            self._boiler_water_c.update({
                name: self.hydronics.water[self.sources[name].loop]
                for name in self._boiler_water_c
            })
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

    def air_temperature_at(self, room: str, x_m: float, y_m: float, z_m: float) -> float:
        """Return the nearest modeled air-cell temperature at room-local coordinates."""
        if room not in self._air_cells:
            raise KeyError(f"Unknown room: {room}")
        nx, ny, nz = self._air_grid_shape[room]
        dx, dy, dz = self._air_cell_size_m[room]
        ix = min(nx - 1, max(0, int(x_m / dx)))
        iy = min(ny - 1, max(0, int(y_m / dy)))
        iz = min(nz - 1, max(0, int(z_m / dz)))
        return self._air_cells[room][iz][iy][ix]

    def diagnostics(self) -> dict:
        return {
            "rooms": {
                name: {"air_c": self._air[name], "fabric_c": self._fabric[name],
                       "fabric_core_c": self._fabric_core[name],
                       "contents_c": self._contents[name],
                       "humidity_pct": relative_humidity(self._air[name], self._moisture[name]),
                       "air_cells_c": [[row.copy() for row in layer]
                                       for layer in self._air_cells[name]],
                       "air_grid": {
                           "shape": list(self._air_grid_shape[name]),
                           "cell_size_m": list(self._air_cell_size_m[name]),
                           "z_centers_m": [(index + .5) * self._air_cell_size_m[name][2]
                                           for index in range(self._air_grid_shape[name][2])],
                       },
                       "air_currents": self._air_currents[name],
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
        for name, angles in plan.airflow_angles_deg.items():
            if (name not in self.sources or self.sources[name].direct_zone is None or len(angles) != 2
                    or not all(isfinite(value) for value in angles)
                    or not -90 <= angles[0] <= 90 or not -60 <= angles[1] <= 75):
                raise ValueError("Invalid A/C airflow direction")
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
        fan_fraction = max(plan.indoor_fans.values(), default=0.0)
        if fan_fraction:
            for room, zone in self.zones.items():
                room_fans = [plan.indoor_fans.get(name, 0.0) for name, source in self.sources.items()
                             if source.direct_zone == room]
                if not room_fans:
                    continue
                cell_capacity = zone.air_capacity / self._cell_count(room)
                mix_rate = 2 * sum(self._grid_conductances(room)[2]) * max(room_fans) / cell_capacity
                jet_rate = 2 * AIR_CELL_JET_KW_K * sqrt(3) * sum(room_fans) / cell_capacity
                max_rate = max(max_rate, self._max_rate + mix_rate + jet_rate)
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
            circulation_rate = self._air_circulation_rate()
            step_s = min(remaining_s, max_step_s,
                         900 / circulation_rate if circulation_rate else 30.0)
            self._step(step_s / 3600, conditions, plan, available_sources)
            self.elapsed_s += step_s
            remaining_s -= step_s
        self.hydronics.exchange(self._air, plan.valves, 0, fabric=self._fabric,
                                outdoor=conditions.outdoor_c, ground=conditions.ground_c,
                                pump_enabled=self._pump_enabled)

    def _air_circulation_rate(self) -> float:
        """Return the fastest per-cell radiator circulation rate (h⁻¹)."""
        outgoing = {}
        for name, detail in self.hydronics.details.items():
            room = self.hydronics.emitters[name].zone
            scale = max(0.0, detail.get("convective_heat_kw", 0.0)) / AIR_RADIATOR_PLUME_DELTA_C
            if not scale:
                continue
            for upstream, _downstream, shape in self._air_circulation_patterns.get(name, ()):
                key = (room, upstream)
                outgoing[key] = outgoing.get(key, 0.0) + shape * scale
        return max((rate / (self.zones[room].air_capacity / self._cell_count(room))
                    for (room, _cell), rate in outgoing.items()), default=0.0)

    def _sample_air_currents(self, room: str, edges):
        """Convert modeled heat-capacity flows to sparse display vectors."""
        nx, ny, nz = self._air_grid_shape[room]
        dx, dy, dz = self._air_cell_size_m[room]
        sums = [[[[0.0, 0.0, 0.0] for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
        counts = [[[[0, 0, 0] for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
        for upstream, downstream, capacity_flow in edges:
            differences = [downstream[index] - upstream[index] for index in range(3)]
            axis = next(index for index, difference in enumerate(differences) if difference)
            direction = 1 if differences[axis] > 0 else -1
            area = (dy * dz, dx * dz, dx * dy)[axis]
            velocity = direction * capacity_flow / (1.2 * AIR_KWH_KG_K * 3600 * area)
            for iz, iy, ix in (upstream, downstream):
                sums[iz][iy][ix][axis] += velocity
                counts[iz][iy][ix][axis] += 1
        cells = [[[[
            sums[iz][iy][ix][axis] / counts[iz][iy][ix][axis]
            if counts[iz][iy][ix][axis] else 0.0
            for axis in range(3)] for ix in range(nx)] for iy in range(ny)] for iz in range(nz)]
        step = 2
        x_centers = [min(nx * dx, (start + min(step, nx - start) / 2) * dx)
                     for start in range(0, nx, step)]
        y_centers = [min(ny * dy, (start + min(step, ny - start) / 2) * dy)
                     for start in range(0, ny, step)]
        vectors = []
        for iz in range(nz):
            layer = []
            for y0 in range(0, ny, step):
                row = []
                for x0 in range(0, nx, step):
                    block = [cells[iz][iy][ix]
                             for iy in range(y0, min(ny, y0 + step))
                             for ix in range(x0, min(nx, x0 + step))]
                    row.append([sum(vector[axis] for vector in block) / len(block)
                                for axis in range(3)])
                layer.append(row)
            vectors.append(layer)
        return {
            "shape": [len(x_centers), len(y_centers), nz],
            "cell_size_m": [dx * step, dy * step, dz],
            "x_centers_m": x_centers,
            "y_centers_m": y_centers,
            "z_centers_m": [(iz + .5) * dz for iz in range(nz)],
            "vectors_m_s": vectors,
        }

    def _step(self, dt_h, conditions, plan, available_sources):
        air_flow = dict.fromkeys(self.zones, 0.0)
        fabric_flow = dict.fromkeys(self.zones, 0.0)
        fabric_core_flow = dict.fromkeys(self.zones, 0.0)
        contents_flow = dict.fromkeys(self.zones, 0.0)
        moisture_flow = dict.fromkeys(self.zones, 0.0)
        air_transport_edges = {name: [] for name in self.zones}
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
        emitter_fabric_heat = self.hydronics.emitter_fabric_heat
        radiator_heat = self.hydronics.radiator_heat
        local_air_sources = {name: [] for name in self.zones}
        fan_sources = {name: [] for name in self.zones}
        for emitter_name, detail in self.hydronics.details.items():
            delivered = detail["room_heat_kw"]
            if delivered:
                local_air_sources[detail["zone"]].append(
                    (delivered, self._air_source_weights[emitter_name])
                )
        fan_mixing = dict.fromkeys(self.zones, 0.0)
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
            fabric_core = self._fabric_core[name]
            coupling = (fabric - air) * zone.air_fabric_kw_k
            contents_coupling = (self._contents[name] - air) * zone.air_contents_kw_k
            air_exchange = zone.infiltration_kw_k + conditions.ventilation_kw_k.get(name, 0)
            ventilation = (outdoor - air) * air_exchange
            moisture_flow[name] = (outdoor_moisture - self._moisture[name]) * air_exchange / AIR_KWH_KG_K
            moisture_flow[name] += conditions.occupied_gains_kw * 0.6 / len(self.zones)
            if zone.fabric_core_capacity_kwh_k:
                surface_core = (fabric_core - fabric) * zone.fabric_core_kw_k
                shell = (outdoor - fabric_core) * zone.fabric_outside_kw_k
                ground = (conditions.ground_c - fabric_core) * zone.ground_kw_k
                surface_shell = (outdoor - fabric) * zone.fabric_surface_outside_kw_k
                fabric_core_flow[name] = -surface_core + shell + ground
            else:
                surface_core = 0.0
                shell = (outdoor - fabric) * zone.fabric_outside_kw_k
                ground = (conditions.ground_c - fabric) * zone.ground_kw_k
                surface_shell = 0.0
            gains = zone.base_gains_kw + appliance_gains
            sun = conditions.solar_kw_m2 * zone.solar_area_m2 + conditions.solar_by_zone_kw.get(name, 0)
            pipe_air = self.hydronics.pipe_air_heat[name]
            pipe_fabric = self.hydronics.pipe_fabric_heat[name]
            air_flow[name] = coupling + contents_coupling + ventilation + gains + emitter_heat[name] + pipe_air
            fabric_flow[name] = (-coupling + surface_core + surface_shell + sun + pipe_fabric
                                 + emitter_fabric_heat[name]
                                 if zone.fabric_core_capacity_kwh_k else
                                 -coupling + shell + ground + sun + pipe_fabric + emitter_fabric_heat[name])
            contents_flow[name] = -contents_coupling
            self._room_flux[name] = {
                "fabric_to_air_kw": coupling, "ventilation_kw": ventilation,
                "envelope_kw": shell + surface_shell,
                "contents_to_air_kw": contents_coupling,
                "ground_kw": ground, "internal_gains_kw": gains, "solar_kw": sun, "emitters_kw": emitter_heat[name],
                "neighbours_kw": 0.0, "direct_heat_kw": 0.0,
                "emitter_radiant_kw": emitter_fabric_heat[name],
                "pipe_to_air_kw": pipe_air, "pipe_to_fabric_kw": pipe_fabric,
                "fabric_surface_core_kw": surface_core,
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
                target_node = self.hydronics.water[loop_name]
                headroom = (water_target - target_node) * loop.capacity / dt_h
                existing_heat = water_flow[loop_name]
                hold = self.boiler_hold_60.get(name)
                if hold is not None:
                    if target_node >= 60.0:
                        hold["latched"] = True
                    if hold["latched"]:
                        water_target = 60.0
                        limit_kw = source.max_heat_kw
                        requested_heat = max(0.0, min(source.max_heat_kw, -existing_heat))
                        request = requested_heat / source.max_heat_kw if source.max_heat_kw else 0.0
                        stopped = not request
                    else:
                        limit_kw = min(limit_kw, max(0.0, headroom - existing_heat))
                else:
                    limit_kw = min(limit_kw, max(0.0, headroom - existing_heat))
                if source.water_hysteresis_c and not (hold and hold["latched"]):
                    threshold = water_target
                    if not self._source_state[name].on:
                        threshold -= source.water_hysteresis_c
                    stopped = (stopped or not request or target_node >= threshold
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
            if delivered and source.direct_zone is None and name in self._pump_overrun:
                # Start circulation in the same step as the water source;
                # otherwise the zero-duration diagnostics refresh still sees
                # a hot boiler request but reports a disconnected, idle loop.
                pump_enabled[loop_name] = True
                self._pump_enabled[loop_name] = True
                self._pump_overrun[name] = source.pump_overrun_s
            latent_kw = 0.0
            fan_kw = 0.0
            sensible = delivered
            if source.direct_zone is None:
                # The boiler heats the same water that is circulating through
                # the loop; there is no separate water-jacket transfer limit.
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
                if sensible:
                    local_air_sources[zone].append(
                        (sensible, self._air_source_weights[name])
                    )
                if fan_kw > 0:
                    fan_mixing[zone] = max(fan_mixing[zone], fan)
                    fan_sources[zone].append((name, fan))
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
                "boiler_function": ("Hold 60°C after first reaching it"
                                    if self.boiler_hold_60.get(name) is not None else "Normal"),
                "boiler_hold_latched": bool(self.boiler_hold_60.get(name)
                                             and self.boiler_hold_60[name]["latched"]),
                "heat_pump_tariff": source.heat_pump_tariff,
                "pump_on": bool(source.direct_zone is None and pump_enabled.get(loop_name, False)),
                "pump_kw": source.pump_kw if source.direct_zone is None and pump_enabled.get(loop_name, False) else 0,
                "boiler_water_c": self.hydronics.water[loop_name] if source.boiler_water_volume_l else None,
                "boiler_water_volume_l": source.boiler_water_volume_l or None,
                "boiler_flow_l_min": self.hydronics.circuits.get(loop_name, {}).get("flow_l_min")
                if source.boiler_water_volume_l else None,
                # Legacy project files may still carry this field, but an
                # in-line boiler has no separate water-to-loop UA.
                "boiler_water_ua_kw_k": None,
                "boiler_effective_water_ua_kw_k": None,
                "boiler_transfer_kw": delivered if source.boiler_water_volume_l else 0.0,
            }

        for name, source in self.sources.items():
            if not source.boiler_water_volume_l or source.direct_zone is not None:
                continue
            boiler_c = self.hydronics.water[source.loop]
            room = source.boiler_room_zone
            ambient = self._air[room] if room else outdoor
            casing_loss = source.boiler_loss_kw_k * (boiler_c - ambient)
            water_flow[source.loop] -= casing_loss
            if room:
                air_flow[room] += casing_loss
                self._room_flux[room]["direct_heat_kw"] += casing_loss
                self._room_flux[room]["boiler_casing_kw"] = casing_loss
            self._boiler_water_c[name] = boiler_c
            self._source_details[name]["boiler_water_c"] = boiler_c
            capacity = (source.boiler_water_volume_l * 4.186 / 3600 +
                        source.boiler_metal_mass_kg * source.boiler_metal_cp_j_kgk / 3_600_000)
            self._source_details[name]["boiler_stored_heat_kwh"] = capacity * (boiler_c - outdoor)
            self._source_details[name]["boiler_casing_loss_kw"] = casing_loss

        opening_cell_flow = {}
        for first, cell_a, second, cell_b, conductance in self._air_opening_edges:
            grid_a, grid_b = self._air_cells[first], self._air_cells[second]
            delta = grid_a[cell_a[0]][cell_a[1]][cell_a[2]] - grid_b[cell_b[0]][cell_b[1]][cell_b[2]]
            flow = conductance * delta
            opening_cell_flow[(first, cell_a)] = opening_cell_flow.get((first, cell_a), 0) - flow
            opening_cell_flow[(second, cell_b)] = opening_cell_flow.get((second, cell_b), 0) + flow
        for first, second, conductance in self._air_opening_conductance:
            vapour = (self._moisture[first] - self._moisture[second]) * conductance / AIR_KWH_KG_K
            moisture_flow[first] -= vapour
            moisture_flow[second] += vapour

        for name, zone in self.zones.items():
            before = self._air[name] - zone.setpoint_c
            previous_air = self._air[name]
            grid = self._air_cells[name]
            nx, ny, nz = self._air_grid_shape[name]
            cell_count = nx * ny * nz
            cell_capacity = zone.air_capacity / cell_count
            mix, buoyancy, fan = self._grid_conductances(name)
            fan = tuple(value * fan_mixing[name] for value in fan)
            circulation_edges = []
            for emitter_name, detail in self.hydronics.details.items():
                if detail["zone"] == name:
                    scale = max(0.0, detail.get("convective_heat_kw", 0.0)) / AIR_RADIATOR_PLUME_DELTA_C
                    for upstream, downstream, shape in self._air_circulation_patterns.get(emitter_name, ()):
                        capacity_flow = shape * scale
                        circulation_edges.append((upstream, downstream, capacity_flow))
                        air_transport_edges[name].append((upstream, downstream, capacity_flow))
            transfer = [[[0.0 for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
            for (room_name, (iz, iy, ix)), flow in opening_cell_flow.items():
                if room_name == name:
                    transfer[iz][iy][ix] += flow
            for iz in range(nz):
                for iy in range(ny):
                    for ix in range(nx):
                        if ix + 1 < nx:
                            rate = (grid[iz][iy][ix] - grid[iz][iy][ix + 1]) * fan[0]
                            transfer[iz][iy][ix] -= rate
                            transfer[iz][iy][ix + 1] += rate
                        if iy + 1 < ny:
                            rate = (grid[iz][iy][ix] - grid[iz][iy + 1][ix]) * fan[1]
                            transfer[iz][iy][ix] -= rate
                            transfer[iz][iy + 1][ix] += rate
                        if iz + 1 < nz:
                            lower, upper = grid[iz][iy][ix], grid[iz + 1][iy][ix]
                            rate = (lower - upper) * fan[2]
                            transfer[iz][iy][ix] -= rate
                            transfer[iz + 1][iy][ix] += rate
                            # Warm air rises from lower cells into cells above.
                            rising = max(0.0, lower - upper) * buoyancy[2]
                            transfer[iz][iy][ix] -= rising
                            transfer[iz + 1][iy][ix] += rising
            # Conservative upwind transport along the divergence-free room
            # vector field. Circulation carries each cell's temperature
            # anomaly along the flow and removes that same energy upstream.
            for upstream, downstream, flow_capacity in circulation_edges:
                flow = flow_capacity * (
                    grid[upstream[0]][upstream[1]][upstream[2]] - previous_air
                )
                transfer[upstream[0]][upstream[1]][upstream[2]] -= flow
                transfer[downstream[0]][downstream[1]][downstream[2]] += flow
            for source_name, fan_fraction in fan_sources[name]:
                horizontal_deg, vertical_deg = plan.airflow_angles_deg.get(
                    source_name, (self.sources[source_name].horizontal_angle_deg,
                                  self.sources[source_name].vertical_angle_deg))
                vector = self._airflow_vector(name, source_name, horizontal_deg, vertical_deg)
                profile = self._air_jet_profile(name, source_name, vector)
                for iz in range(nz):
                    for iy in range(ny):
                        for ix in range(nx):
                            for axis, next_index in ((0, (iz, iy, ix + 1)),
                                                     (1, (iz, iy + 1, ix)),
                                                     (2, (iz + 1, iy, ix))):
                                if ((axis == 0 and ix + 1 >= nx)
                                        or (axis == 1 and iy + 1 >= ny)
                                        or (axis == 2 and iz + 1 >= nz)):
                                    continue
                                first = (iz, iy, ix)
                                direction = vector[axis]
                                if abs(direction) < 1e-6:
                                    continue
                                upstream, downstream = (first, next_index) if direction > 0 else (next_index, first)
                                weight = (profile[upstream[0]][upstream[1]][upstream[2]]
                                          + profile[downstream[0]][downstream[1]][downstream[2]]) / 2
                                conductance = AIR_CELL_JET_KW_K * fan_fraction * abs(direction) * weight
                                air_transport_edges[name].append((upstream, downstream, conductance))
                                flow = (grid[upstream[0]][upstream[1]][upstream[2]]
                                        - grid[downstream[0]][downstream[1]][downstream[2]]) * conductance
                                transfer[upstream[0]][upstream[1]][upstream[2]] -= flow
                                transfer[downstream[0]][downstream[1]][downstream[2]] += flow
            source_delta = [[[0.0 for _ in range(nx)] for _ in range(ny)] for _ in range(nz)]
            for heat_kw, weights in local_air_sources[name]:
                for iz in range(nz):
                    for iy in range(ny):
                        for ix in range(nx):
                            source_delta[iz][iy][ix] += heat_kw * (
                                weights[iz][iy][ix] - 1 / cell_count)
            cell_exchange = (zone.air_fabric_kw_k + zone.air_contents_kw_k + zone.infiltration_kw_k
                             + conditions.ventilation_kw_k.get(name, 0.0)) / cell_count
            updated = []
            for iz in range(nz):
                layer = []
                for iy in range(ny):
                    row = []
                    for ix in range(nx):
                        difference = previous_air - grid[iz][iy][ix]
                        local_exchange = cell_exchange * difference
                        rate = (air_flow[name] / cell_count + source_delta[iz][iy][ix]
                                + local_exchange + transfer[iz][iy][ix])
                        row.append(grid[iz][iy][ix] + rate * dt_h / cell_capacity)
                    layer.append(row)
                updated.append(layer)
            self._air_cells[name] = updated
            # Stable conservative exchange for room-scale turbulent mixing.
            # Pairwise exact relaxation avoids forcing every house subsystem
            # to use sub-second steps just because air cells mix quickly.
            mix_fraction = tuple(
                .5 * -expm1(-2 * conductance * dt_h / cell_capacity)
                for conductance in mix
            )
            for axis, fraction in enumerate(mix_fraction):
                if fraction <= 0:
                    continue
                for iz in range(nz):
                    for iy in range(ny):
                        for ix in range(nx):
                            first = (iz, iy, ix)
                            next_index = [iz, iy, ix]
                            next_index[(2, 1, 0)[axis]] += 1
                            if next_index[0] >= nz or next_index[1] >= ny or next_index[2] >= nx:
                                continue
                            second = tuple(next_index)
                            difference = (self._air_cells[name][first[0]][first[1]][first[2]]
                                          - self._air_cells[name][second[0]][second[1]][second[2]])
                            exchanged = difference * fraction
                            self._air_cells[name][first[0]][first[1]][first[2]] -= exchanged
                            self._air_cells[name][second[0]][second[1]][second[2]] += exchanged
            self._air[name] = sum(sum(map(sum, layer)) for layer in self._air_cells[name]) / cell_count
            self._air_currents[name] = self._sample_air_currents(name, air_transport_edges[name])
            self._fabric[name] += fabric_flow[name] * dt_h / zone.fabric_capacity
            if zone.fabric_core_capacity_kwh_k:
                self._fabric_core[name] += (fabric_core_flow[name] * dt_h
                                            / zone.fabric_core_capacity_kwh_k)
            if zone.contents_capacity_kwh_k:
                self._contents[name] += contents_flow[name] * dt_h / zone.contents_capacity_kwh_k
            humidity = self._moisture[name] + moisture_flow[name] * dt_h / dry_air_kg[name]
            self._moisture[name] = min(humidity_ratio(self._air[name],100), max(0,humidity))
            after = self._air[name] - zone.setpoint_c
            area = (abs(before) + abs(after)) * dt_h / 2
            if before * after < 0:
                area *= (before**2 + after**2) / (after - before)**2
            if zone.conditioned:
                self.totals.comfort_degree_hours += area
        self.hydronics.advance(dt_h, water_flow, slab_heat, radiator_heat)
        for name, source in self.sources.items():
            if source.boiler_water_volume_l and source.direct_zone is None:
                boiler_c = self.hydronics.water[source.loop]
                self._boiler_water_c[name] = boiler_c
                self._source_details[name]["boiler_water_c"] = boiler_c
                capacity = (source.boiler_water_volume_l * 4.186 / 3600 +
                            source.boiler_metal_mass_kg * source.boiler_metal_cp_j_kgk / 3_600_000)
                self._source_details[name]["boiler_stored_heat_kwh"] = (
                    capacity * (boiler_c - conditions.outdoor_c)
                )
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
            "room_air_net_kw": sum(air_flow.values()),
            "fabric_net_kw": sum(fabric_flow.values()) + sum(fabric_core_flow.values()),
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

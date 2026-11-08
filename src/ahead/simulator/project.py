"""Compile an editable house and experiment into the simulator's runtime model."""

from bisect import bisect_right
from collections import defaultdict
from dataclasses import replace
from datetime import datetime, timedelta
from math import isfinite
from re import fullmatch

from ahead.control import HysteresisController
from ahead.simulator.floorplan import compile_pipes, derive_envelope, pipe_paths, validate_plan
from ahead.simulator.pipe_network import PipeNetwork
from ahead.simulator.house import Conditions, House, OutdoorUnit, Partition, Source, Zone
from ahead.simulator.hydronics import Emitter, HeatExchanger, WaterLoop
from ahead.simulator.presets import PRESETS
from ahead.simulator.scenario import Fault, Scenario
from ahead.simulator.solar import incident_kw_m2, sun_vector


SETTINGS = {
    "latitude": 48.1, "longitude": 20.8, "orientation_deg": 0.0,
    "start": "2026-01-15T00:00:00+01:00", "ground_c": 10.0,
    "weather_period_s": 86400, "duration_s": 259200, "step_s": 60,
    "warmup_days": 0, "noise_c": 0.06, "seed": 42,
}


def _values(raw, defaults, label):
    if not isinstance(raw, dict) or raw.keys() - defaults.keys():
        raise ValueError(f"{label} contains unsupported fields")
    values = defaults | raw
    for key, value in values.items():
        default = defaults[key]
        if type(default) is bool:
            valid = type(value) is bool
        elif type(default) is int:
            valid = type(value) is int
        elif type(default) is float:
            valid = type(value) in (int, float) and isfinite(value)
        else:
            valid = isinstance(value, str)
        if not valid:
            raise ValueError(f"{label}.{key} has an invalid value")
    return values


def _rows(project):
    result = {}
    for collection, presets in PRESETS.items():
        values = project.get(collection, [])
        if not isinstance(values, list):
            raise ValueError(f"{collection} must be a list")
        result[collection] = rows = {}
        for raw in values:
            if not isinstance(raw, dict):
                raise ValueError(f"{collection} entries must be objects")
            name = raw.get("id")
            preset = raw.get("preset")
            if not isinstance(name, str) or not fullmatch(r"[a-zA-Z0-9_-]{1,64}", name) or name in rows:
                raise ValueError(f"{collection} needs unique IDs containing letters, digits, _ or -")
            if not isinstance(preset, str) or preset not in presets:
                raise ValueError(f"Choose a valid preset for {collection}/{name}")
            fields = {key: value for key, value in raw.items() if key not in ("id", "preset")}
            rows[name] = _values(fields, presets[preset], f"{collection}/{name}") | {"preset": preset}
    return result


def _positive(*numbers):
    if any(value <= 0 for value in numbers):
        raise ValueError("Sizes, capacities and design flow rates must be positive")


def _fraction(*numbers):
    if any(not 0 <= value <= 1 for value in numbers):
        raise ValueError("Fractions must be between zero and one")


def _reference(row, field, items):
    value = row[field]
    if value not in items:
        raise ValueError(f"{field} references an unknown object: {value or '(empty)'}")
    return value


def validate_draft(project: dict) -> tuple[dict, dict]:
    if not isinstance(project, dict) or project.keys() - {"settings", "floorplan", *PRESETS}:
        raise ValueError("Project contains unknown sections")
    validate_plan(project.get("floorplan", {}))
    return _values(project.get("settings", {}), SETTINGS, "settings"), _rows(project)


def compile_project(project: dict) -> tuple[Scenario, dict]:
    settings, rows = validate_draft(project)
    start = datetime.fromisoformat(settings["start"])
    if start.utcoffset() is None:
        raise ValueError("Start time needs a UTC offset, for example +01:00")
    if not (-90 <= settings["latitude"] <= 90 and -180 <= settings["longitude"] <= 180):
        raise ValueError("Latitude and longitude are out of range")
    step = settings["step_s"]
    duration = settings["duration_s"]
    period = settings["weather_period_s"]
    if step <= 0 or duration <= 0 or period < 0 or (period and period % step):
        raise ValueError("Run duration and step must be positive and the weather period aligned")
    if settings["noise_c"] < 0 or not 0 <= settings["warmup_days"] <= 14:
        raise ValueError("Noise must be non-negative and warm-up between zero and 14 days")

    plan = project.get("floorplan", {})
    rectangles = derive_envelope(plan, rows)
    room_rows = rows["rooms"]
    surfaces = rows["surfaces"]
    layers = defaultdict(list)
    windows = defaultdict(list)
    for row in rows["layers"].values():
        surface = _reference(row, "surface", surfaces)
        _positive(row["thickness_m"], row["conductivity_w_mk"], row["density_kg_m3"], row["heat_capacity_j_kgk"])
        layers[surface].append(row)
    for row in rows["windows"].values():
        surface = _reference(row, "surface", surfaces)
        _positive(row["area_m2"], row["u_w_m2k"])
        _fraction(row["shade"], row["solar_transmittance"])
        windows[surface].append(row)

    volume = {}
    envelope = defaultdict(float)
    ground = defaultdict(float)
    coupling = defaultdict(float)
    mass = defaultdict(float)
    partitions = []
    surface_info = {}
    for name, row in room_rows.items():
        level = rows["levels"][_reference(row, "level", rows["levels"])]
        height = row["height_m"] or level["height_m"]
        _positive(row["length_m"], row["width_m"], height)
        volume[name] = row["length_m"] * row["width_m"] * height

    for name, row in surfaces.items():
        room = _reference(row, "room", room_rows)
        boundary = row["boundary"]
        if boundary not in {"outdoors", "ground", "room", "adiabatic"}:
            raise ValueError(f"Unknown boundary type for {name}: {boundary}")
        _positive(row["area_m2"])
        _fraction(row["shade"])
        if not 0 <= row["tilt_deg"] <= 180 or row["u_override"] < 0:
            raise ValueError(f"Invalid tilt or U-value override for {name}")
        opaque = row["area_m2"] - sum(item["area_m2"] for item in windows[name])
        if opaque < 0:
            raise ValueError(f"Windows exceed the area of {name}")
        resistance = 0.17 + sum(layer["thickness_m"] / layer["conductivity_w_mk"] for layer in layers[name])
        u_value = row["u_override"] or 1 / resistance
        u_area = u_value * opaque
        u_area += sum(item["area_m2"] * item["u_w_m2k"] for item in windows[name])
        conductance = u_area / 1000
        material_mass = sum(
            layer["thickness_m"] * layer["density_kg_m3"] * layer["heat_capacity_j_kgk"]
            for layer in layers[name]
        ) * opaque / 3_600_000
        azimuth = (row["azimuth_deg"] + settings["orientation_deg"]) % 360
        surface_info[name] = {
            "room": room, "adjacent_room": row["adjacent_room"] or None, "boundary": boundary,
            "azimuth_deg": azimuth, "tilt_deg": row["tilt_deg"], "area_m2": row["area_m2"],
            "opaque_m2": opaque, "u_w_m2k": u_value, "conductance_kw_k": conductance,
            "material_capacity_kwh_k": material_mass,
        }
        share = 0.5 if boundary == "room" else 1.0
        mass[room] += material_mass * share
        coupling[room] += row["area_m2"] * 0.003 * share
        if boundary == "room":
            adjacent = _reference(row, "adjacent_room", room_rows)
            mass[adjacent] += material_mass * 0.5
            coupling[adjacent] += row["area_m2"] * 0.0015
            if conductance:
                partitions.append(Partition(room, adjacent, conductance))
        elif boundary == "outdoors":
            envelope[room] += conductance
        elif boundary == "ground":
            ground[room] += conductance

    zones = []
    for name, row in room_rows.items():
        external = envelope[name] + ground[name]
        # Split the steady-state surface conductance across the air-to-fabric
        # and fabric-to-environment links without counting resistance twice.
        air_to_fabric = max(coupling[name], 2 * external)
        scale = air_to_fabric / (air_to_fabric - external) if external else 1
        zones.append(Zone(
            name, volume[name] * 1.2 * 1.005 / 3600, mass[name] + row["internal_mass_kwh_k"],
            air_to_fabric, envelope[name] * scale, volume[name] * row["ach"] * 1.2 * 1.005 / 3600,
            row["initial_c"], row["target_c"], base_gains_kw=row["gains_kw"],
            ground_kw_k=ground[name] * scale, conditioned=row["preset"] != "unheated",
            expose_sensor=row["expose_sensor"], initial_humidity_pct=row["initial_humidity_pct"],
        ))
        for surface in surface_info.values():
            if surface["room"] == name:
                surface["model_conductance_kw_k"] = (
                    surface["conductance_kw_k"] * scale if surface["boundary"] in {"outdoors", "ground"}
                    else surface["conductance_kw_k"]
                )
    loops = {
        name: WaterLoop(row["volume_l"] * 4.186 / 3600, row["initial_c"], row["target_c"],
                        row["loss_kw_k"], row["max_flow_l_min"],
                        row["preset"] == "hot_water_cylinder", row["mains_c"], row["expose_sensors"])
        for name, row in rows["circuits"].items()
    }
    emitters = {}
    for name, row in rows["emitters"].items():
        room = _reference(row, "room", room_rows)
        circuit = _reference(row, "circuit", loops)
        if loops[circuit].dhw:
            raise ValueError("Emitters must use a space-heating circuit")
        if row["preset"] == "underfloor":
            area = row["area_m2"]
            _positive(area, row["pipe_spacing_m"], row["loop_length_m"], row["slab_depth_m"])
            if row["finish_r_m2k_w"] < 0:
                raise ValueError("Floor finish resistance must be non-negative")
            pipe_conductance = min(area, row["loop_length_m"] * row["pipe_spacing_m"]) * 0.008
            slab_capacity = area * row["slab_depth_m"] * 2300 * 880 / 3_600_000
            room_conductance = area / (0.12 + row["finish_r_m2k_w"]) / 1000
            emitters[name] = Emitter(room, circuit, pipe_conductance, row["flow_l_min"],
                                     slab_capacity, room_conductance, row["travel_s"], row["local_sensor_warming_c"],
                                     row["local_sensor_bias_c"], row["report_valve_opening"])
        else:
            _positive(row["rated_kw"], row["rated_delta_k"], row["width_m"], row["height_m"])
            # Scale the generic rating from the preset's reference size.
            reference = PRESETS["emitters"][row["preset"]]
            reference_area = reference["width_m"] * reference["height_m"]
            output = row["rated_kw"] * row["width_m"] * row["height_m"] / reference_area
            emitters[name] = Emitter(room, circuit, output / row["rated_delta_k"], row["flow_l_min"],
                                     travel_s=row["travel_s"], local_sensor_warming_c=row["local_sensor_warming_c"],
                                     local_sensor_bias_c=row["local_sensor_bias_c"],
                                     report_valve_opening=row["report_valve_opening"])

    outdoor_units = {}
    for name, row in rows["outdoor_units"].items():
        fields = {key: value for key, value in row.items() if key != "preset"}
        outdoor_units[name] = OutdoorUnit(**fields)

    sources = []
    for name, row in rows["sources"].items():
        preset = row["preset"]
        if preset == "indoor_split":
            room = _reference(row, "room", room_rows)
            unit = _reference(row, "outdoor_unit", outdoor_units)
            sources.append(Source(name, "electricity", row["capacity_kw"], 1, direct_zone=room,
                                  modulating=True, reversible=row["reversible"], outdoor_unit=unit,
                                  room_setpoint_control=row["room_setpoint_control"],
                                  fan_control=row["fan_control"], report_heat=row["report_heat"],
                                  indoor_fan_kw=row["indoor_fan_kw"], report_indoor_humidity=row["report_indoor_humidity"],
                                  humidity_setpoint_control=row["humidity_setpoint_control"]))
        elif preset == "air_to_water":
            sources.append(Source(
                name, "electricity", row["capacity_kw"], row["cop"], modulating=True,
                efficiency_per_outdoor_k=row["cop_per_outdoor_k"], min_on_s=row["min_on_s"],
                min_off_s=row["min_off_s"], loop=_reference(row, "circuit", loops),
                efficiency_per_water_k=row["cop_per_water_k"],
                water_setpoint_control=row["water_setpoint_control"],
                dhw_loop=row["dhw_circuit"] or None, report_heat=row["report_heat"],
            ))
        else:
            sources.append(Source(
                name, "gas", row["capacity_kw"], row["efficiency"], modulating=preset == "gas_modulating",
                min_on_s=row["min_on_s"], min_off_s=row["min_off_s"],
                loop=_reference(row, "circuit", loops), min_fraction=row.get("minimum_fraction", 0),
                water_setpoint_control=row.get("water_setpoint_control", False),
                dhw_loop=row["dhw_circuit"] or None, report_heat=row["report_heat"],
            ))
    exchangers = {
        name: HeatExchanger(_reference(row, "from_circuit", loops), _reference(row, "to_circuit", loops),
                            row["kw_k"], row["max_heat_kw"], row["pump_kw"])
        for name, row in rows["heat_exchangers"].items()
    }
    pipes, nodes = compile_pipes(plan, rows, rectangles)
    paths = pipe_paths(pipes, nodes, emitters, {source.name: source for source in sources})
    pipe_network = PipeNetwork(pipes, paths) if pipes else None
    house = House(tuple(zones), tuple(partitions), tuple(sources), loops=loops, emitters=emitters,
                  outdoor_units=outdoor_units, exchangers=exchangers, pipe_network=pipe_network)

    for row in rows["solar"].values():
        face = _reference(row, "surface", surfaces)
        if surfaces[face]["boundary"] != "outdoors":
            raise ValueError("Solar equipment requires an outdoor surface")
        _positive(row["area_m2"])
        if row["preset"] == "photovoltaic":
            _fraction(row["efficiency"])
            _positive(row["inverter_kw"])
        else:
            _reference(row, "circuit", loops)
            _fraction(row["optical_efficiency"])
            if row["loss_w_m2k"] < 0:
                raise ValueError("Collector losses must be non-negative")

    weather = sorted(rows["weather"].values(), key=lambda item: item["start_s"])
    times = [row["start_s"] for row in weather]
    if not times or times[0] != 0 or len(times) != len(set(times)) or (period and times[-1] >= period):
        raise ValueError("Weather must start at zero with unique times inside its repeat period")
    profile = tuple(
        (row["start_s"], Conditions(
            row["outdoor_c"], occupied_gains_kw=row["occupancy_kw"], appliance_kw=row["appliance_kw"],
            electricity_price_kwh=row["electricity_price"], gas_price_kwh=row["gas_price"],
            forecast_outdoor_c=row["forecast_outdoor_c"], ground_c=settings["ground_c"],
            outdoor_humidity_pct=row["outdoor_humidity_pct"],
        )) for row in weather
    )
    if any(row[key] < 0 for row in weather
           for key in ("direct_kw_m2", "diffuse_kw_m2", "occupancy_kw", "appliance_kw")):
        raise ValueError("Weather irradiance and heat gains must be non-negative")
    if any(not 0 <= row["outdoor_humidity_pct"] <= 100 for row in weather):
        raise ValueError("Outdoor humidity must be between zero and 100")
    if any(time % step for time in times):
        raise ValueError("Weather changes must align with the simulation step")

    events = []
    faults = []
    for row in rows["events"].values():
        event = row["preset"]
        beginning = row["start_s"]
        ending = beginning + row["duration_s"]
        if beginning < 0 or ending <= beginning or ending > duration or beginning % step or ending % step:
            raise ValueError("Events must be positive and aligned inside the run duration")
        if event in ("window_open", "extractor_on", "door_open", "room_sensor_dropout"):
            _reference(row, "room", room_rows)
        if event in ("window_open", "extractor_on"):
            _positive(row["extra_ach"])
        if event == "door_open":
            if row["radiant_area_m2"] < 0:
                raise ValueError("Door radiant area must be non-negative")
            _fraction(row["radiant_exchange_factor"])
            _reference(row, "adjacent_room", room_rows)
            _positive(row["kw_k"])
            if row["room"] == row["adjacent_room"]:
                raise ValueError("A door must connect distinct rooms")
        if event == "hot_water_draw":
            circuit = _reference(row, "circuit", loops)
            if not loops[circuit].dhw:
                raise ValueError("Hot-water draw requires a hot-water cylinder")
            _positive(row["flow_l_min"])
        if event == "room_sensor_dropout":
            faults.append(Fault("zone:" + row["room"], beginning, ending))
        elif event == "outdoor_sensor_bias":
            faults.append(Fault("outdoor", beginning, ending, row["bias_c"]))
        elif event == "source_outage":
            faults.append(Fault("source:" + _reference(row, "source", house.sources), beginning, ending))
        else:
            events.append((beginning, ending, row))
    fault_end = {}
    for fault in sorted(faults, key=lambda fault: fault.start_s):
        if fault.start_s < fault_end.get(fault.target, 0):
            raise ValueError("Faults targeting the same device cannot overlap")
        fault_end[fault.target] = fault.end_s

    diagnostics = {
        "surfaces": surface_info, "solar": {}, "ground_c": settings["ground_c"],
        "configured_objects": rows,
        "floorplan": plan,
        "routed_emitters": sorted(paths),
    }

    def environment(elapsed, conditions):
        if elapsed < 0:
            profile_time = elapsed % period if period else 0
            conditions = profile[bisect_right(times, profile_time) - 1][1]
        time_in_profile = elapsed % period if period else max(0, elapsed)
        row = weather[bisect_right(times, time_in_profile) - 1]
        # Updating the sun each minute is sufficient for the thermal time step.
        sun = sun_vector(start + timedelta(seconds=elapsed - elapsed % 60),
                         settings["latitude"], settings["longitude"])
        sunlight = defaultdict(float)
        irradiance = {}
        for name, surface in surface_info.items():
            incident = 0.0
            if surface["boundary"] == "outdoors":
                incident = incident_kw_m2(
                    sun, surface["azimuth_deg"], surface["tilt_deg"], row["direct_kw_m2"],
                    row["diffuse_kw_m2"], surfaces[name]["shade"],
                )
            irradiance[name] = incident
            solar_gain = sum(
                incident * window["area_m2"] * window["solar_transmittance"] * (1 - window["shade"])
                for window in windows[name]
            )
            sunlight[surface["room"]] += solar_gain
            surface["irradiance_kw_m2"] = incident
            surface["window_solar_kw"] = solar_gain
        pv = 0.0
        collectors = {}
        for name, device in rows["solar"].items():
            light = irradiance[device["surface"]] * device["area_m2"]
            details = {"surface": device["surface"], "incident_kw": light, "preset": device["preset"]}
            if device["preset"] == "photovoltaic":
                output = min(device["inverter_kw"], light * device["efficiency"])
                pv += output
                details["electrical_kw"] = output
            else:
                circuit = device["circuit"]
                collectors[name] = (circuit, light * device["optical_efficiency"],
                                    device["loss_w_m2k"] * device["area_m2"] / 1000)
                details["circuit"] = circuit
            diagnostics["solar"][name] = details

        ventilation = defaultdict(float)
        doors = defaultdict(float)
        radiation = defaultdict(float)
        draws = defaultdict(float)
        for beginning, ending, event in events:
            if not beginning <= elapsed < ending:
                continue
            kind = event["preset"]
            if kind in ("window_open", "extractor_on"):
                room = event["room"]
                ventilation[room] += volume[room] * event["extra_ach"] * 1.2 * 1.005 / 3600
            elif kind == "door_open":
                pair = tuple(sorted((event["room"], event["adjacent_room"])))
                doors[pair] += event["kw_k"]
                if event["radiant_area_m2"]:
                    radiation[pair] += event["radiant_area_m2"] * event["radiant_exchange_factor"]
            else:
                draws[event["circuit"]] += event["flow_l_min"]
        diagnostics["sun"] = {"east": sun[0], "north": sun[1], "up": sun[2]}
        diagnostics["disturbances"] = {
            "extra_ventilation_kw_k": dict(ventilation),
            "door_exchange_kw_k": {" / ".join(key): value for key, value in doors.items()},
            "hot_water_draw_l_min": dict(draws),
        }
        return replace(conditions, solar_by_zone_kw=sunlight, solar_thermal=collectors, solar_pv_kw=pv,
                       ventilation_kw_k=ventilation, door_exchange_kw_k=doors,
                       radiant_opening_m2=radiation, dhw_draw_l_min=draws)

    scenario = Scenario(house, profile, step, duration, period or None, tuple(faults),
                        settings["noise_c"], settings["seed"], environment)
    return scenario, diagnostics


def prepare_project(project: dict) -> tuple[Scenario, dict]:
    scenario, diagnostics = compile_project(project)
    days = project.get("settings", {}).get("warmup_days", 0)
    if days:
        end = days * 86400
        original_environment = scenario.environment
        warmup = replace(
            scenario, duration_s=end, step_s=60, faults=(), sensor_noise_c=0,
            environment=lambda seconds, conditions: original_environment(seconds - end, conditions),
        )
        thermostat = HysteresisController(scenario.house.equipment, scenario.setpoints_c, 0.5)
        for _ in warmup.run(thermostat):
            pass
        scenario.house.reset_run_clock()
    return scenario, diagnostics

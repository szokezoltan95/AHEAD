"""On-device TRV and indoor HVAC controls for the live simulator."""

from dataclasses import dataclass
from math import exp, isfinite, pi, sin

from ahead.control import ControlPlan


FAN_SPEEDS = {"low": 0.4, "medium": 0.65, "high": 1.0}
TRV_PRESETS = ("manual", "adaptive")
AC_PRESETS = ("eco", "sleep", "boost")
SENSOR_SOURCES = ("built_in", "room_sensor", "external_input")
TRV_NUMBERS = {
    "external_temperature": ("External temperature input", -30, 65, .1),
    "sensor_offset": ("Sensor correction", -10, 10, .1),
    "overshoot": ("Early cut-off correction", 0, 5, .1),
}


@dataclass
class ClimateUnit:
    key: str
    kind: str
    name: str
    zone: str
    target: float
    modes: tuple
    fan_control: bool = False
    vertical_swing_available: bool = False
    horizontal_swing_available: bool = False
    vertical_angle_deg: float = 30.0
    horizontal_angle_deg: float = 0.0
    vertical_swing_amplitude_deg: float = 25.0
    horizontal_swing_amplitude_deg: float = 45.0
    vertical_swing_period_s: float = 8.0
    horizontal_swing_period_s: float = 12.0
    mode: str = "off"
    preset: str = "manual"
    fan: str = "auto"
    swing: str = "off"
    humidity_target: float = 55
    sensor_source: str = "built_in"
    sensor_offset: float = 0
    overshoot: float = 0
    external_temperature: float | None = None
    external_time: float = float("-inf")
    local_warming: float = 0
    previous_s: float = 0
    demand: float = 0
    learned: float = 0
    coast_until_s: float = 0
    coast_peak: float = float("-inf")

    @property
    def swing_modes(self):
        modes = ["off"]
        if self.vertical_swing_available:
            modes.append("vertical")
        if self.horizontal_swing_available:
            modes.append("horizontal")
        if self.vertical_swing_available and self.horizontal_swing_available:
            modes.append("both")
        return tuple(modes)

    @property
    def has_swing(self):
        return self.vertical_swing_available or self.horizontal_swing_available

    def apply(self, field, payload, elapsed_s):
        value = payload.decode().strip()
        choices = {
            "mode": self.modes,
            "preset": ("none", *(TRV_PRESETS if self.kind == "trv" else AC_PRESETS)),
            "fan": ("auto", *FAN_SPEEDS), "swing": self.swing_modes, "sensor_source": SENSOR_SOURCES,
        }
        if field == "swing" and value == "on":
            value = "both" if "both" in self.swing_modes else next(
                (mode for mode in self.swing_modes if mode != "off"), "off")
        if field in choices:
            if value not in choices[field]:
                raise ValueError("Unsupported setting")
            if field == "mode" or field == "preset":
                self.demand = 0
                self.coast_until_s = 0
            if field == "preset" and value == "none" and self.kind == "trv":
                value = "manual"
            setattr(self, field, value)
            return
        angle_fields = {"vertical_angle": ("vertical_angle_deg", -60, 75),
                        "horizontal_angle": ("horizontal_angle_deg", -90, 90)}
        limits = {"target": (7, 35), "humidity_target": (35, 80)}
        limits.update({key: (low, high) for key, (_, low, high, _) in TRV_NUMBERS.items()})
        limits.update({key: (low, high) for key, (_, low, high) in angle_fields.items()})
        if field in angle_fields and self.kind != "ac":
            raise ValueError("Louver settings are only available on A/C units")
        low, high = limits[field]
        number = float(value)
        if not isfinite(number) or not low <= number <= high:
            raise ValueError("Climate setting outside supported range")
        setattr(self, angle_fields[field][0] if field in angle_fields else field, number)
        if field == "external_temperature":
            self.external_time = elapsed_s
        if field == "target":
            self.coast_until_s = 0

    def trv_request(self, observation, house, room):
        elapsed = observation.elapsed_s
        emitter = house.hydronics.details.get(self.name, {})
        heat_kw = max(0, emitter.get("room_heat_kw", 0))
        device = house.hydronics.emitters[self.name]
        desired_warming = device.local_sensor_warming_c * min(1, heat_kw)
        factor = 1 - exp(-max(0, elapsed-self.previous_s) / 120)
        self.local_warming += factor * (desired_warming-self.local_warming)
        self.previous_s = elapsed
        built_in = room + self.local_warming + device.local_sensor_bias_c
        external = self.external_temperature if elapsed-self.external_time <= 900 else None
        readings = {
            "built_in": built_in, "room_sensor": observation.indoor_c.get(self.zone),
            "external_input": external,
        }
        used = self.sensor_source
        measured = readings[used]
        if used == "external_input" and measured is None:
            used, measured = "built_in", built_in
        control_temp = measured + self.sensor_offset if measured is not None else None
        effective_target = self.target - self.overshoot
        adaptive = self.preset == "adaptive"
        if adaptive:
            effective_target -= self.learned
        previous = self.demand
        self.demand = 0.0
        if self.mode == "heat" and control_temp is not None:
            error = effective_target - control_temp
            if adaptive:
                if error >= (0.12 if previous else 0.3):
                    self.demand = min(1, max(.15, error / 1.25))
            elif (previous and error > 0) or error >= 0.3:
                self.demand = 1.0
            if adaptive and previous and not self.demand:
                self.coast_until_s = elapsed+600
                self.coast_peak = control_temp
            elif adaptive and self.coast_until_s:
                self.coast_peak = max(self.coast_peak, control_temp)
                if elapsed >= self.coast_until_s or self.demand:
                    overshoot = max(0, min(2, self.coast_peak-self.target))
                    self.learned = 0.7*self.learned + 0.3*overshoot
                    self.coast_until_s = 0
        else:
            self.coast_until_s = 0
        return self.demand, {
            "built_in_c": built_in, "built_in_bias_c": device.local_sensor_bias_c,
            "local_warming_c": self.local_warming, "room_sensor_c": readings["room_sensor"],
            "external_c": external, "control_c": control_temp, "sensor_source": used,
            "selected_sensor_source": self.sensor_source, "sensor_offset_c": self.sensor_offset,
            "overshoot_correction_c": self.overshoot, "learned_overshoot_c": self.learned,
            "effective_target_c": effective_target,
        }

    def ac_request(self, temperature, humidity):
        limit = {"eco": .65, "sleep": .55}.get(self.preset, 1.0)
        offset = 1 if self.preset == "sleep" else 0
        demand = 0.0
        if temperature is not None:
            previous = self.demand
            heat_error = self.target-offset-temperature
            cool_error = temperature-self.target-offset
            deadband = .7 if self.mode == "auto" else .35
            heat_threshold = .1 if previous > 0 else deadband
            cool_threshold = .1 if previous < 0 else deadband
            if self.mode in ("heat", "auto") and heat_error > heat_threshold:
                demand = min(limit, max(.15, heat_error/2))
            elif self.mode in ("cool", "auto") and cool_error > cool_threshold:
                demand = -min(limit, max(.15, cool_error/2))
            elif (self.mode == "dry" and humidity is not None and temperature > self.target-2
                  and humidity > self.humidity_target+(1 if previous < 0 else 3)):
                demand = -min(limit,.45)
        self.demand = demand
        automatic = .4+.6*abs(demand) if demand else .55
        fan = FAN_SPEEDS.get(self.fan,automatic)
        if self.preset == "boost" and self.fan == "auto":
            fan = 1.0
        elif self.preset == "sleep" and self.fan == "auto":
            fan = min(fan,.45)
        return demand, fan


class ClimateControls:
    def __init__(self, house, root):
        self.house = house
        self.root = root
        self.upstream = {name: [name] for name in house.hydronics.loops}
        for destination, loops in self.upstream.items():
            for circuit in loops:
                for exchanger in house.exchangers.values():
                    if exchanger.destination == circuit and exchanger.source not in loops:
                        loops.append(exchanger.source)
        self.units = {}
        for name, emitter in house.hydronics.emitters.items():
            if emitter.fixed_open:
                continue
            key = "trv_"+name
            self.units[key] = ClimateUnit(key,"trv",name,emitter.zone,
                self.target(emitter.zone),("off","heat"))
        for name, source in house.sources.items():
            if source.direct_zone is not None:
                key = "ac_"+name
                modes = ("off", "heat")
                if source.fan_control:
                    modes += ("fan_only",)
                if source.reversible:
                    modes += ("cool","auto","dry")
                self.units[key] = ClimateUnit(key,"ac",name,source.direct_zone,
                    self.target(source.direct_zone),modes,fan_control=source.fan_control,preset="none",
                    vertical_swing_available=source.vertical_swing_available,
                    horizontal_swing_available=source.horizontal_swing_available,
                    vertical_angle_deg=source.vertical_angle_deg,
                    horizontal_angle_deg=source.horizontal_angle_deg,
                    vertical_swing_amplitude_deg=source.vertical_swing_amplitude_deg,
                    horizontal_swing_amplitude_deg=source.horizontal_swing_amplitude_deg,
                    vertical_swing_period_s=source.vertical_swing_period_s,
                    horizontal_swing_period_s=source.horizontal_swing_period_s)
        self.commands = {
            self.topic(unit,field,"set"): (unit,field)
            for unit in self.units.values() for field in self.mqtt_fields(unit)
        }

    def target(self, zone):
        return min(35,max(7,self.house.zones[zone].setpoint_c))

    def topic(self, unit, field, suffix="state"):
        return f"{self.root}/climate/{unit.key}/{field}/{suffix}"

    def mqtt_fields(self, unit):
        if unit.kind == "trv":
            return ("mode", "target", "preset", "sensor_source", "external_temperature", "sensor_offset")
        fields = ("mode", "target")
        if unit.fan_control:
            fields += ("preset", "fan")
        if unit.has_swing:
            fields += ("swing",)
        return fields + (("humidity_target",) if self.house.sources[unit.name].humidity_setpoint_control
                         and "dry" in unit.modes else ())

    def remote_catalog(self):
        catalog = {}
        for unit in self.units.values():
            fields = {
                "mode": {"label":"Mode","options":list(unit.modes)},
                "target": {"label":"Target temperature","min":7,"max":35,"step":.5,"unit":"°C"},
                "preset": {"label":"Preset","options":list(TRV_PRESETS if unit.kind == "trv" else ("none",*AC_PRESETS))},
            }
            if unit.kind == "trv":
                fields["sensor_source"] = {"label":"Temperature sensor","options":list(SENSOR_SOURCES)}
                for name,(label,low,high,step) in TRV_NUMBERS.items():
                    fields[name] = {"label":label,"min":low,"max":high,"step":step,"unit":"°C"}
            else:
                if not unit.fan_control:
                    fields.pop("preset")
                else:
                    fields["fan"] = {"label":"Fan speed","options":["auto",*FAN_SPEEDS]}
                if unit.has_swing:
                    fields["swing"] = {"label":"Louver swing","options":list(unit.swing_modes)}
                fields["vertical_angle"] = {"label":"Vertical louver direction","min":-60,"max":75,"step":1,"unit":"°"}
                fields["horizontal_angle"] = {"label":"Horizontal louver direction","min":-90,"max":90,"step":1,"unit":"°"}
                if self.house.sources[unit.name].humidity_setpoint_control and "dry" in unit.modes:
                    fields["humidity_target"] = {"label":"Target humidity","min":35,"max":80,"step":1,"unit":"%"}
            catalog[unit.key] = {"group":"emitters" if unit.kind == "trv" else "sources",
                                 "device":unit.name,"fields":fields}
        return catalog

    def components(self, unit, identifier, availability):
        paired = {"mode":"mode", "target":"temperature", "preset":"preset_mode"}
        if unit.kind == "ac":
            if unit.fan_control:
                paired["fan"] = "fan_mode"
            else:
                paired.pop("preset")
            if unit.has_swing:
                paired["swing"] = "swing_mode"
            if self.house.sources[unit.name].humidity_setpoint_control and "dry" in unit.modes:
                paired["humidity_target"] = "target_humidity"
        climate = {
            "platform":"climate", "name":None, "unique_id":identifier+"_"+unit.key,
            "modes":list(unit.modes), "preset_modes":list(TRV_PRESETS if unit.kind == "trv" else AC_PRESETS),
            "min_temp":7, "max_temp":35, "temp_step":.5, "temperature_unit":"C",
            "action_topic":self.topic(unit,"action"), "qos":1, "retain":False,
            "availability":availability, "availability_mode":"all",
        }
        # Do not publish room-average simulation values as an HVAC sensor.
        # TRV temperature is its own local device measurement; room sensors are
        # represented separately by explicitly placed temperature sensors.
        if unit.kind == "trv":
            climate["current_temperature_topic"] = self.topic(unit,"current")
        for field, prefix in paired.items():
            climate[prefix+"_command_topic"] = self.topic(unit,field,"set")
            climate[prefix+"_state_topic"] = self.topic(unit,field)
        components = {unit.key:climate}
        if unit.kind == "ac":
            if unit.fan_control:
                climate.update(fan_modes=["auto",*FAN_SPEEDS])
            else:
                climate.pop("preset_modes")
            if unit.has_swing:
                climate["swing_modes"] = list(unit.swing_modes)
            if self.house.sources[unit.name].report_indoor_humidity:
                climate["current_humidity_topic"] = self.topic(unit,"humidity")
            if "humidity_target" in paired:
                climate.update(min_humidity=35,max_humidity=80)
        else:
            for field in ("external_temperature", "sensor_offset"):
                label, low, high, step = TRV_NUMBERS[field]
                key = unit.key+"_"+field
                components[key] = {
                    "platform":"number","name":label,"unique_id":identifier+"_"+key,
                    "state_topic":self.topic(unit,field),"command_topic":self.topic(unit,field,"set"),
                    "availability":availability,"min":low,"max":high,"step":step,
                    "unit_of_measurement":"°C","qos":1,"retain":False,
                }
            key = unit.key+"_sensor_source"
            components[key] = {
                "platform":"select","name":"Temperature sensor source","unique_id":identifier+"_"+key,
                "options":list(SENSOR_SOURCES),"state_topic":self.topic(unit,"sensor_source"),
                "command_topic":self.topic(unit,"sensor_source","set"),"availability":availability,"qos":1,
            }
            if self.house.hydronics.emitters[unit.name].report_valve_opening:
                key = unit.key+"_opening"
                components[key] = {
                    "platform":"sensor","name":"Actual valve opening","unique_id":identifier+"_"+key,
                    "state_topic":self.topic(unit,"opening"),"unit_of_measurement":"%",
                    "state_class":"measurement","availability":availability,
                }
        return components

    def plan(self, observation, raw):
        sources, valves = {}, {}
        fans, dry, fan_only, stop = {}, set(), set(), set()
        airflow_angles = {}
        states = {}
        calls = {}
        temperatures = self.house.indoor_c
        for key, unit in self.units.items():
            if unit.kind == "trv":
                demand, sensor = unit.trv_request(observation,self.house,temperatures[unit.zone])
                manual = unit.name in raw["valve"]
                if not manual and (demand or unit.mode != "off"):
                    valves[unit.name] = demand
                if demand and not manual:
                    loop = self.house.hydronics.emitters[unit.name].loop
                    calls[loop] = max(calls.get(loop,0),demand)
                states[key] = sensor | {"request":demand,"override":manual}
            else:
                temperature = observation.indoor_c.get(unit.zone)
                humidity = observation.indoor_humidity.get(unit.zone)
                demand, fan = unit.ac_request(temperature,humidity)
                manual = unit.name in raw["source"]
                if not manual and unit.name in observation.available_sources:
                    if unit.mode != "off":
                        source = self.house.sources[unit.name]
                        sources[unit.name] = demand if source.modulating or not demand else (1.0 if demand > 0 else -1.0)
                        if unit.fan_control or source.reversible:
                            fans[unit.name] = fan
                    if unit.mode == "fan_only":
                        fan_only.add(unit.name)
                    if unit.mode in ("off","fan_only"):
                        stop.add(unit.name)
                    if unit.mode == "dry":
                        dry.add(unit.name)
                vertical_angle = unit.vertical_angle_deg
                horizontal_angle = unit.horizontal_angle_deg
                if unit.swing in ("vertical", "both"):
                    vertical_angle += unit.vertical_swing_amplitude_deg * sin(
                        2 * pi * observation.elapsed_s / unit.vertical_swing_period_s)
                if unit.swing in ("horizontal", "both"):
                    horizontal_angle += unit.horizontal_swing_amplitude_deg * sin(
                        2 * pi * observation.elapsed_s / unit.horizontal_swing_period_s)
                vertical_angle = max(-60, min(75, vertical_angle))
                horizontal_angle = max(-90, min(90, horizontal_angle))
                airflow_angles[unit.name] = (horizontal_angle, vertical_angle)
                states[key] = {"control_c":temperature,"humidity_pct":humidity,"request":demand,
                               "fan_fraction":fan,"override":manual,"swing":unit.swing,
                               "vertical_angle":unit.vertical_angle_deg,
                               "horizontal_angle":unit.horizontal_angle_deg,
                               "airflow_vertical_deg":vertical_angle,
                               "airflow_horizontal_deg":horizontal_angle}
            states[key].update(mode=unit.mode,preset=unit.preset,target_c=unit.target,
                               humidity_target=unit.humidity_target,fan=unit.fan,
                               zone=unit.zone,kind=unit.kind,device=unit.name)
        for loop, demand in calls.items():
            eligible = (
                (name, source) for upstream in self.upstream[loop]
                for name, source in self.house.sources.items()
                if source.direct_zone is None and source.automatic_heat_calls and source.loop == upstream
                and name in observation.available_sources and raw["source"].get(name) != 0
            )
            selected = next(eligible, None)
            if selected:
                name, source = selected
                request = max(.3,demand) if source.modulating else 1.0
                sources[name] = max(sources.get(name,0),request)
        return ControlPlan(
            sources | raw["source"], valves | raw["valve"],
            room_setpoints_c=raw["room_setpoint"], fan_fractions=raw["fan"],
            water_setpoints_c=raw["water_setpoint"], indoor_fans=fans, dry_sources=frozenset(dry),
            climate_states=states, fan_only_sources=frozenset(fan_only), stopped_sources=frozenset(stop),
            airflow_angles_deg=airflow_angles,
        )

    def publish(self, client, sample):
        for key, unit in self.units.items():
            state = sample.plan.climate_states.get(key)
            if state is None:
                continue
            values = {"mode":state["mode"],"target":state["target_c"],"preset":state["preset"],
                      "current":state["control_c"]}
            if unit.kind == "trv":
                emitter = self.house.hydronics.details.get(unit.name,{})
                values.update(sensor_source=state["selected_sensor_source"],
                    sensor_offset=state["sensor_offset_c"],external_temperature=state["external_c"])
                if self.house.hydronics.emitters[unit.name].report_valve_opening:
                    values["opening"] = emitter.get("opening",0)*100
                heat = emitter.get("room_heat_kw",0)
                if state["mode"] == "off" and not state["override"]:
                    action = "off"
                else:
                    action = "heating" if heat > .005 and emitter.get("flow_l_min",0) > 0 else "idle"
            else:
                source = self.house.source_details.get(unit.name,{})
                output = source.get("heat_kw",0)
                if unit.fan_control:
                    values["fan"] = state["fan"]
                else:
                    values.pop("preset")
                if unit.has_swing:
                    values["swing"] = state["swing"]
                values.update(vertical_angle=state["vertical_angle"],
                              horizontal_angle=state["horizontal_angle"])
                model = self.house.sources[unit.name]
                if model.report_indoor_humidity:
                    values["humidity"] = state["humidity_pct"]
                if model.humidity_setpoint_control and "dry" in unit.modes:
                    values["humidity_target"] = state["humidity_target"]
                action = "off" if state["mode"] == "off" and not output else "idle"
                if output > .005:
                    action = "heating"
                elif output < -.005:
                    action = "drying" if state["mode"] == "dry" else "cooling"
                elif source.get("fan_kw",0) > .0005:
                    action = "fan"
            values["action"] = action
            for field, value in values.items():
                payload = "None" if value is None else value if isinstance(value,str) else f"{value:.3f}"
                client.publish(self.topic(unit,field),payload,retain=True)

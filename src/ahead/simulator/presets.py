"""Editable generic starting assumptions, expressed in the editor's SI units."""

MATERIALS = {
    "brick": (0.55, 1600, 840),
    "block": (0.23, 700, 900),
    "concrete": (1.70, 2300, 880),
    "timber": (0.14, 550, 1600),
    "gypsum": (0.25, 850, 1090),
    "eps": (0.038, 20, 1450),
    "xps": (0.035, 35, 1450),
    "mineral_wool": (0.037, 35, 1030),
    "tile": (1.10, 2200, 850),
    "laminate": (0.16, 750, 1500),
    "roof_tile": (0.90, 1900, 900),
}

PRESETS = {
    "levels": {"storey": {"height_m": 2.7, "elevation_m": 0.0}},
    "rooms": {"conditioned": {"level": "", "length_m": 5.0, "width_m": 4.0,
                             "height_m": 0.0, "initial_c": 20.0, "target_c": 21.0,
                             "ach": 0.3, "internal_mass_kwh_k": 1.2,
                             "air_contents_kw_k": 0.25, "gains_kw": 0.03},
              "unheated": {"level": "", "length_m": 4.0, "width_m": 3.0,
                           "height_m": 0.0, "initial_c": 17.0, "target_c": 17.0,
                           "ach": 0.6, "internal_mass_kwh_k": 0.8,
                           "air_contents_kw_k": 0.08, "gains_kw": 0.0}},
    "surfaces": {
        "wall": {"room": "", "adjacent_room": "", "boundary": "outdoors", "area_m2": 12.0,
                 "azimuth_deg": 180.0, "tilt_deg": 90.0, "shade": 0.0, "u_override": 0.0},
        "floor": {"room": "", "adjacent_room": "", "boundary": "ground", "area_m2": 20.0,
                  "azimuth_deg": 0.0, "tilt_deg": 180.0, "shade": 0.0, "u_override": 0.0},
        "ceiling": {"room": "", "adjacent_room": "", "boundary": "outdoors", "area_m2": 20.0,
                    "azimuth_deg": 0.0, "tilt_deg": 0.0, "shade": 0.0, "u_override": 0.0},
        "roof": {"room": "", "adjacent_room": "", "boundary": "outdoors", "area_m2": 28.0,
                 "azimuth_deg": 180.0, "tilt_deg": 35.0, "shade": 0.0, "u_override": 0.0},
    },
    "layers": {name: {"surface": "", "thickness_m": 0.10, "conductivity_w_mk": data[0],
                      "density_kg_m3": data[1], "heat_capacity_j_kgk": data[2]}
               for name, data in MATERIALS.items()},
    "windows": {
        "exterior_door": {"surface": "", "area_m2": 2.0, "u_w_m2k": 1.4, "solar_transmittance": 0.0, "shade": 0.0},
        "double_glazed": {"surface": "", "area_m2": 2.0, "u_w_m2k": 1.5, "solar_transmittance": 0.55, "shade": 0.0},
        "triple_glazed": {"surface": "", "area_m2": 2.0, "u_w_m2k": 0.9, "solar_transmittance": 0.45, "shade": 0.0},
    },
    "circuits": {
        "radiator_loop": {"volume_l": 75.0, "initial_c": 25.0, "target_c": 50.0,
                          "loss_kw_k": 0.006, "max_flow_l_min": 16.0, "mains_c": 10.0},
        "floor_loop": {"volume_l": 65.0, "initial_c": 22.0, "target_c": 34.0,
                       "loss_kw_k": 0.004, "max_flow_l_min": 12.0, "mains_c": 10.0},
        "buffer_tank": {"volume_l": 200.0, "initial_c": 30.0, "target_c": 55.0,
                        "loss_kw_k": 0.002, "max_flow_l_min": 18.0, "mains_c": 10.0},
        "hot_water_cylinder": {"volume_l": 150.0, "initial_c": 45.0, "target_c": 55.0,
                               "loss_kw_k": 0.002, "max_flow_l_min": 15.0, "mains_c": 10.0},
    },
    "heat_exchangers": {
        "tank_to_loop": {"from_circuit": "", "to_circuit": "", "kw_k": 0.22,
                         "max_heat_kw": 8.0, "pump_kw": 0.045},
    },
    "emitters": {
        "panel_radiator": {"room": "", "circuit": "", "rated_kw": 1.7, "rated_delta_k": 50.0,
                           "width_m": 1.0, "height_m": 0.6, "flow_l_min": 5.0, "travel_s": 90.0},
        "column_radiator": {"room": "", "circuit": "", "rated_kw": 1.3, "rated_delta_k": 50.0,
                            "width_m": 0.8, "height_m": 0.6, "flow_l_min": 4.0, "travel_s": 90.0},
        "towel_radiator": {"room": "", "circuit": "", "rated_kw": 0.6, "rated_delta_k": 50.0,
                            "width_m": 0.5, "height_m": 1.2, "flow_l_min": 2.5, "travel_s": 90.0},
        "underfloor": {"room": "", "circuit": "", "area_m2": 15.0, "pipe_spacing_m": 0.15,
                       "loop_length_m": 100.0, "flow_l_min": 2.0, "slab_depth_m": 0.06,
                       "finish_r_m2k_w": 0.04, "travel_s": 120.0},
    },
    "sources": {
        "gas_on_off": {"circuit": "", "capacity_kw": 12.0, "efficiency": 0.86,
                       "min_on_s": 120.0, "min_off_s": 180.0, "dhw_circuit": "", "water_hysteresis_c": 0.0, "pump_start_c": 35.0, "pump_stop_c": 30.0, "pump_overrun_s": 180.0, "pump_kw": 0.06},
        "gas_modulating": {"circuit": "", "capacity_kw": 16.0, "efficiency": 0.93,
                           "minimum_fraction": 0.22, "min_on_s": 120.0, "min_off_s": 180.0,
                           "water_setpoint_control": True, "dhw_circuit": ""},
        "air_to_water": {"circuit": "", "capacity_kw": 8.0, "cop": 3.5,
                         "cop_per_outdoor_k": 0.08, "cop_per_water_k": -0.045,
                         "min_on_s": 240.0, "min_off_s": 180.0, "water_setpoint_control": True,
                         "dhw_circuit": ""},
        "indoor_split": {"room": "", "outdoor_unit": "", "capacity_kw": 2.5, "reversible": True,
                         "room_setpoint_control": True, "fan_control": True, "indoor_fan_kw": 0.045,
                         "report_indoor_humidity": False, "humidity_setpoint_control": False, "heat_pump_tariff": False},
    },
    "outdoor_units": {
        "split": {"capacity_kw": 3.5, "cop": 3.4, "cop_per_outdoor_k": 0.08,
                  "capacity_per_outdoor_k": 0.06, "defrost_period_s": 3600,
                  "defrost_duration_s": 240, "defrost_below_c": 3.0, "defrost_power_kw": 0.6,
                  "mode_rule": "largest_demand", "allocation": "proportional"},
        "multi_split": {"capacity_kw": 5.0, "cop": 3.2, "cop_per_outdoor_k": 0.08,
                        "capacity_per_outdoor_k": 0.08, "defrost_period_s": 3600,
                        "defrost_duration_s": 240, "defrost_below_c": 3.0, "defrost_power_kw": 0.8,
                        "mode_rule": "largest_demand", "allocation": "proportional"},
    },
    "solar": {
        "photovoltaic": {"surface": "", "area_m2": 10.0, "efficiency": 0.19, "inverter_kw": 2.0},
        "thermal_collector": {"surface": "", "circuit": "", "area_m2": 4.0,
                              "optical_efficiency": 0.68, "loss_w_m2k": 4.0},
    },
    "weather": {"period": {"start_s": 0, "outdoor_c": 0.0, "forecast_outdoor_c": 0.0,
                           "outdoor_humidity_pct": 75.0, "direct_kw_m2": 0.45, "diffuse_kw_m2": 0.10, "occupancy_kw": 0.25,
                           "appliance_kw": 0.25, "electricity_price": 0.24, "gas_price": 0.09, "heat_pump_price": -1.0}},
    "events": {
        "window_open": {"start_s": 3600, "duration_s": 600, "room": "", "extra_ach": 2.0},
        "extractor_on": {"start_s": 3600, "duration_s": 600, "room": "", "extra_ach": 1.5},
        "door_open": {"start_s": 3600, "duration_s": 1800, "room": "", "adjacent_room": "", "kw_k": 0.2,
                      "radiant_area_m2": 0.0, "radiant_exchange_factor": 0.7,
                      "opening_fraction": 1.0},
        "hot_water_draw": {"start_s": 3600, "duration_s": 420, "circuit": "", "flow_l_min": 7.0},
        "room_sensor_dropout": {"start_s": 3600, "duration_s": 1800, "room": ""},
        "outdoor_sensor_bias": {"start_s": 3600, "duration_s": 1800, "bias_c": 2.0},
        "source_outage": {"start_s": 3600, "duration_s": 1800, "source": ""},
    },
}

for preset in PRESETS["rooms"].values():
    preset["expose_sensor"] = True
    preset["initial_humidity_pct"] = 50.0
    preset["floor_offset_m"] = 0.0
for name, preset in PRESETS["emitters"].items():
    preset["local_sensor_warming_c"] = 1.5 if name != "underfloor" else 0.0
    preset["local_sensor_bias_c"] = 0.0
    preset["report_valve_opening"] = False
    preset["fixed_open"] = False
for name, preset in PRESETS["sources"].items():
    preset["report_heat"] = name in {"gas_modulating", "air_to_water"}
    if name != "indoor_split":
        preset["automatic_heat_calls"] = True
for preset in PRESETS["circuits"].values():
    preset["expose_sensors"] = True

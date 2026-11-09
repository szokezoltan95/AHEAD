# AHEAD

**Adaptive Heating and Energy Automation Development**

AHEAD is an early-stage research project on self-adaptive control of building heating. It will investigate whether a controller can learn how an unfamiliar building heats up and cools down from routine measurements, then use that model to coordinate its heating equipment while maintaining comfort and respecting equipment limits.

> **Project status:** The Compose test bench runs a real-time whole-house simulator that Home Assistant can control via MQTT. It includes a browser-based floor-plan editor and live schematic. The adaptive AHEAD controller and its HAOS integration are planned.

## Research scope

- **Equipment integration.** Describe devices by their available measurements and commands so the same controller can work with different combinations of boilers, heat pumps, reversible air conditioners, radiators, and thermostatic radiator valves.
- **Online system identification.** Estimate building thermal dynamics and equipment response during operation, with minimal manual calibration.
- **Adaptive and predictive control.** Compare strategies that update the building model as conditions change, including adaptive model predictive control (MPC).
- **Hybrid heating optimisation.** Decide when to use each available heat source based on weather, energy tariffs, estimated efficiency, comfort requirements, and equipment limits.
- **Partial observability.** Evaluate how the controller performs when quantities such as delivered heat, valve position, or internal equipment modulation are unavailable or measured indirectly.

## Proposed architecture

| Component | Role |
| --- | --- |
| Adaptive control engine | Estimates thermal dynamics, predicts the effect of control actions, and chooses equipment commands subject to comfort and equipment constraints. The algorithms are packaged as a reusable Python library so they can also be exercised in simulation and tests. |
| Home Assistant integration | Runs the full heating controller locally on Home Assistant OS (HAOS), including online learning, predictive control, and heat-source optimisation. It reads equipment and sensor entities, issues device commands, and provides configuration and monitoring in Home Assistant. |
| Physics-based simulator | Models building heat transfer and thermal inertia, heating equipment, hydraulic flow, actuator response, and weather. Virtual devices expose the measurements and controls available in real installations while the simulator keeps its internal parameters hidden from the controller. |
| Experiment infrastructure | Runs repeatable, containerised research experiments on a proposed dedicated server. Scenarios vary building properties, weather, energy tariffs, equipment configurations, and sensor faults. |
| Physical systems | Provide a later stage of validation on real equipment where feasible. |

A standard AHEAD deployment is intended to require only the AHEAD integration on HAOS. The control engine runs within Home Assistant Core, with its dependencies installed by Home Assistant. Simulation and benchmark infrastructure are separate research tools. The deployed controller requires no separate AHEAD service or cloud backend.

## Run the live Home Assistant test bench

Install Docker with Compose and run this from the repository root.

```bash
docker compose up -d --build
```

Compose starts the simulator, an MQTT broker, Home Assistant Container, and a localhost web proxy. The three lab services share an isolated Docker network without a route to the home LAN or the internet. Only the proxy publishes ports on the Docker host. It forwards the two web interfaces without changing HTTP headers or WebSocket traffic. The simulator advances one simulated second per wall-clock second and publishes fresh readings every two seconds. It runs until stopped. The example repeats its daily weather and tariff profile, and its scheduled faults occur once at the specified elapsed times.

1. Open [Home Assistant](http://localhost:8123) and complete onboarding.
2. In **Settings → Devices & services → Add integration**, select **MQTT**. Enter broker host `mqtt` and port `1883`. Leave the username and password blank. The broker is on the Compose network and its port is unpublished.
3. Under **Settings → Devices & services → MQTT**, open a simulated device such as **Kitchen TRV**. Select **Heat** and set a temperature above the current reading. The TRV opens its valve. A source configured for automatic heat calls can respond to the request. The boiler circulation pump starts with the burner, stops after a 3-minute overrun when water falls to 30 °C, and starts on water temperature above 35 °C. The pump draws 60 W while running. Watch the valve, boiler output and supply temperature in the simulator.

### Simulated climate controls

- Each emitter with a controllable thermostat has a heat-only climate entity. A fixed-open radiator has no thermostat or valve control. **Manual** uses a fixed thermostat response. **Adaptive** tracks temperature after the valve closes and learns an early cut-off correction from repeated overshoot. The simulator lets you tune **Early cut-off correction** independently of the signed **Sensor correction**.
- A TRV's **Temperature sensor source** can use its **built-in** reading, the simulated **room sensor**, or an **external input** written from Home Assistant. The built-in sensor can have a fixed bias and warm up near an active radiator. External inputs expire after 15 simulated minutes and fall back to the built-in sensor. The thermostat diagnostics show which value was used.
- A reversible room unit offers **Heat**, **Cool**, **Auto**, **Dry**, **Fan only** and **Off**. It modulates demand from the temperature error and reports the actual operating action after applying equipment limits. Fan settings affect electrical draw. Dry mode uses a simplified room-moisture and coil model. The swing setting is reported as device state. The room model treats air as mixed, so it has no directional airflow calculation.
- Open **Live**, select an emitter, indoor unit, or heat source under **Equipment**, and use **Device controls** as its local remote or thermostat. Changes are applied to the running house and reflected in Home Assistant. Climate units start **Off** on a new run. Boilers configured for manual control have **Off** and **On** settings. Where a boiler accepts automatic heat calls, return it to **Auto** in Live or press **Resume automatic heat calls** in Home Assistant.
- Home Assistant receives only the controls and readings exposed by the simulated equipment profile. A typical TRV exposes setpoint, mode, sensor selection and correction, and heating action. Actual valve opening is available to Home Assistant when enabled on the emitter profile. Sensor warming, learned overshoot, actual valve travel and detailed compressor measurements are visible in the simulator. For an A/C profile, indoor humidity reporting and target humidity control are optional.

Use `docker compose logs -f simulator` to see connections and received requests. Use `docker compose down` to stop the bench. Home Assistant keeps its configuration in a Docker volume. The simulated house starts from the scenario's initial state when the simulator restarts.

Set `AHEAD_SCENARIO` in the Compose `.env` file to load a TOML scenario or a floor-plan project JSON at startup. For a private project, save it as `private-house.json` beside `compose.yaml` and set `AHEAD_SCENARIO=./private-house.json`. An existing applied project takes precedence. To switch an existing installation, import the JSON in **Configure** and choose **Apply and restart simulation**. The web ports are published on all server network interfaces. Allow ports 8123 and 8765 in the server firewall, then open `http://<server-ip>:8123` for Home Assistant and `http://<server-ip>:8765` for the simulator. If you changed `AHEAD_HA_PORT` or `AHEAD_CONSOLE_PORT`, use those port numbers instead. Home Assistant still discovers the virtual devices through the internal MQTT broker.

## Configure and monitor the house

Open the [AHEAD simulator console](http://localhost:8765). The **History** page graphs room temperature and humidity, radiator opening, boiler and A/C output, pump state, and water temperatures. It retains up to seven days of one-minute samples while the simulator process runs. The floor-plan Heat flow view overlays a live air-temperature grid, emitter activity and room-to-room and envelope heat-flow arrows; its grid is a visual approximation from the modeled room temperatures and active equipment. Compose publishes the console on all server interfaces. Set `AHEAD_CONSOLE_PORT` in `.env` to use a different host port.

- **Live** shows selectable water circuits as a piping schematic, with separate supply and return lines, junctions, and flow-direction arrows. Switch to **Floor plan** for room temperatures and positioned equipment, or **Heat flow** for directional transfers in watts. The equipment list includes devices without a floor-plan position. Inspect each component's live readings and recent trends, and the underlying JSON at `/api/live`. Local device commands also accept JSON with `device`, `field`, and `value` at `POST /api/remote`. Room air and fabric temperatures, valve positions and flow, water and slab temperatures, requested and actual heat output, sensor faults, and the heat and electrical balances are visible independently of MQTT.
- **Configure** opens with an editable six-room example. Draw rooms on the floor plan, click wall edges to set construction and openings, and place radiators and other equipment. Use **Advanced configuration** for detailed parameters. Choose **Save draft** or **Apply and restart simulation**. Applying validates links, optionally warms up the house, stores the starting thermal checkpoint, and begins a new live run. You can import and export project JSON. After applying, use the refreshed virtual devices in Home Assistant. A reversible indoor unit also retains its raw signed request control for experiments.
- **Replay** uses the same saved project, starting checkpoint, and seeded sensor noise. Applying a project or restarting the simulator starts its scenario clock from zero. Home Assistant commands are live inputs, so repeating an experiment also requires repeating the controller inputs.

To route radiators, place a boiler or circuit connection and a radiator. Select **Draw pipe**, choose the circuit and **Supply + return**, and click the connection points in order. Use **Add junction** to branch from a run. Switching floors before choosing the next point draws a riser. In **Select / move**, click a segment to set its mounting, reference wall, centreline offset or depth, and endpoint heights above each floor. Shift-click to select several segments. Live pipe readings include length, flow, inlet and outlet temperatures, and heat exchanged with the surroundings.

In **Heat flow**, arrows show room-to-room air exchange, conduction and radiation, plus heat loss through the envelope. A door-open event includes an **Opening fraction (0–1)** control in **Configure → Advanced configuration → Occupancy events and faults**; for example, 0.35 means 35% open. The default house keeps its two exterior doors closed and models interior doors partially open.

Rooms drawn from scratch generate wall areas and shared surfaces. Existing rooms can be placed visually while keeping their manually configured construction. The editor accepts a rectangular plan for each room. A radiator uses drawn pipe heat losses when it has both a supply and return route to a source or circuit connection. The model still distributes water using the configured branch flows and circuit limit.

| Area | Configurable model |
| --- | --- |
| Construction | Levels, heated or unheated rooms, layered walls/floors/ceilings/roofs, windows and doors, orientation, shared boundaries and ground |
| Hydronics | Multiple circuits, radiator sizes, moving valves, underfloor slabs, buffer tanks, heat exchangers and hot-water cylinders with draw events and optional source priority |
| Equipment | On/off and modulating boilers, air-to-water heat pumps, and single- or multi-split air-to-air systems with shared capacity and configurable mode arbitration |
| Solar and electrical | Surface irradiance from the scenario location, time and orientation, glazing gains, solar thermal, PV, and electrical import and export |
| Scenarios | Actual and forecast weather, occupancy and tariffs, ventilation and open-door events, hot-water draws, sensor faults and equipment outages |

On first start without an override, the live bench uses the bundled TOML house. The applied project takes precedence on later starts and is stored in the `simulator-config` volume.

An indoor split with `heat_pump_tariff` enabled uses a separate meter. Set `heat_pump_price` in each weather period in the same units as the ordinary electricity rate. The heat-pump meter includes compressor, indoor fan and defrost electricity. House PV offsets only ordinary household demand. The meters report separately in Live and via MQTT.

The solar position calculation uses the [NOAA fractional-year approximation](https://gml.noaa.gov/grad/solcalc/solareqns.PDF). These are generic simulation presets. The room fabric and water tanks use lumped thermal states, with conductances derived from configured assemblies. The model uses simplified flow allocation, shading and equipment curves. It has yet to be calibrated against a measured building.

The Compose test bench hosts virtual equipment for development and experiments. The planned AHEAD integration will run the controller locally on HAOS when deployed to a real home.

## Run an offline benchmark

With Python 3.12 or later, run from the repository root.

```bash
python -m pip install -e ".[live]"
python -m ahead.simulator scenarios/hybrid_house.toml --csv hybrid-house.csv
python -m unittest discover -s tests -v
```

The offline example runs three winter days in a synthetic, two-storey, four-zone house. A room heat pump and a gas boiler with thermostatic radiator valves respond to the baseline controller. The scenario includes daily weather, solar gains, occupancy and appliance loads, time-varying tariffs, a room sensor dropout, an outdoor sensor bias, and a heat-pump outage.

| Model | Implemented behaviour |
| --- | --- |
| Building | Configurable rooms with separate air and fabric thermal mass, heat exchange across walls and floors, envelope losses, infiltration, and solar and internal gains |
| Heating | Direct room heating and a shared hydronic loop with a finite thermal capacity, flow-limited radiators, valve requests, and supply and return temperatures |
| Equipment | On/off and modulating requests, source capacity, gas efficiency, outdoor-dependent heat-pump COP, equipment availability, and minimum on/off times |
| Experiment | TOML scenarios, seeded sensor noise, timed faults, and a CSV trace with ground truth, sensor readings, actuator requests, and cumulative energy and cost |

The simulator advances the physical model between controller decisions. The controller receives sensor readings, equipment availability, and tariffs through the shared interface. The simulator records the underlying state separately so controller performance can be scored against it. Energy use includes the configured electrical appliance load. Comfort deviation is the sum of absolute room setpoint errors over time.

The TOML benchmark uses the original four-zone house and one water loop. The browser project editor supports the broader model described above. The included house and weather profiles are synthetic.

## Evaluation

AHEAD will compare adaptive strategies against conventional thermostatic control and rule-based hybrid heating. A central experiment will measure how quickly the controller adapts to previously unseen buildings and how its performance changes when measurements are missing or faulty.

| Area | Measures |
| --- | --- |
| Comfort | Cumulative temperature deviation from the comfort target or band (°C·h) |
| Energy and cost | Energy use (kWh) and operating cost under the scenario’s tariffs |
| Model performance | Prediction error and time to adapt to an unfamiliar building |
| Equipment operation | Cycling and adherence to operating limits |
| Robustness and runtime | Performance under incomplete feedback or sensor faults, and computational load |

[BOPTEST](https://ibpsa.github.io/project1-boptest/index.html) is a candidate for reference benchmarks or an alternative simulation backend. [EnergyPlus](https://energyplus.readthedocs.io/en/latest/) is a candidate for cross-checking simplified thermal models.

## Proposed roadmap

| Period | Focus |
| --- | --- |
| Months 1–4 | Architecture, literature review, and simulation framework |
| Months 5–8 | Virtual equipment, Home Assistant integration, and baseline control |
| Months 9–14 | Online identification and adaptive control |
| Months 15–19 | Automated comparative experiments and optimisation |
| Months 20–24 | Validation, publications, documentation, and software release |

Planned outputs include an open-source control library and Home Assistant integration, a configurable simulator, reproducible benchmark scenarios, comparative datasets, and research publications. The platform is also intended to support student research, diploma theses, and laboratory exercises.

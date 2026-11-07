# AHEAD

**Adaptive Heating and Energy Automation Development**

AHEAD is an early-stage research project on self-adaptive control of building heating. It will investigate whether a controller can learn how an unfamiliar building heats up and cools down from routine measurements, then use that model to coordinate its heating equipment while maintaining comfort and respecting equipment limits.

> **Project status:** The Compose test bench runs a real-time whole-house simulator that Home Assistant can control via MQTT. The adaptive AHEAD controller and its HAOS integration are planned.

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

Compose starts the simulator, an MQTT broker, and Home Assistant Container. The simulator advances one simulated second per wall-clock second and publishes fresh readings every two seconds. It runs until stopped. The example repeats its daily weather and tariff profile, and its scheduled faults occur once at the specified elapsed times.

1. Open [Home Assistant](http://localhost:8123) and complete onboarding.
2. In **Settings → Devices & services → Add integration**, select **MQTT**. Enter broker host `mqtt` and port `1883`. Leave the username and password blank. The broker is on the Compose network and its port is unpublished.
3. Open the discovered **AHEAD house** device. Set **Kitchen valve** to 100% and switch **Gas Boiler request** on. Watch **Supply water temperature**, **Gas Boiler heat output**, and **Kitchen temperature**. Heat output and temperature evolve according to the model. You can use the same entities in Home Assistant automations.

The device exposes room and outdoor temperature sensors, supply and return temperatures, loop flow, tariffs, energy use, and heat output. On/off sources use switches. Modulating sources and valves use percentage controls. All requests start at zero and are set by Home Assistant. An equipment outage makes the affected source control unavailable, while sensor dropouts appear as unknown readings.

Use `docker compose logs -f simulator` to see connections and received requests. Use `docker compose down` to stop the bench. Home Assistant keeps its configuration in a Docker volume. The simulated house starts from the scenario's initial state when the simulator restarts.

Set `AHEAD_SCENARIO` in a Compose `.env` file to mount a different TOML scenario. The web UI binds to loopback. For a remote Docker host, forward its Home Assistant port to your computer with SSH.

The Compose test bench hosts virtual equipment for development and experiments. The planned AHEAD integration will run the controller locally on HAOS when deployed to a real home.

## Run an offline benchmark

With Python 3.12 or later, run from the repository root.

```bash
python -m pip install -e .
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

The current model uses configurable thermal parameters for each room and a lumped water loop. Geometry-driven construction, pressure-based hydraulics, domestic hot water, and cooling are outside the current implementation. The included house and weather profiles are synthetic and have yet to be calibrated against a measured building.

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

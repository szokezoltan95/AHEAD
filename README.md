# AHEAD

**Adaptive Heating and Energy Automation Development**

AHEAD is an early-stage research project on self-adaptive control of building heating. It will investigate whether a controller can learn how an unfamiliar building heats up and cools down from routine measurements, then use that model to coordinate its heating equipment while maintaining comfort and respecting equipment limits.

> **Project status:** Research and design. The controller, simulator, and Home Assistant integration are planned deliverables.

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

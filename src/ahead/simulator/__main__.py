"""Run a scenario with the baseline controller and optionally export a trace."""

import argparse
from contextlib import nullcontext
import csv
from dataclasses import asdict
import json
from pathlib import Path

from ahead.control import HysteresisController
from ahead.simulator.scenario import load_scenario


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", type=Path)
    parser.add_argument("--csv", type=Path, help="Write an interval trace")
    args = parser.parse_args()

    try:
        scenario = load_scenario(args.scenario)
        house = scenario.house
        controller = HysteresisController(house.equipment, scenario.setpoints_c, deadband_c=0.5)
        zones = list(house.zones)
        sources = list(house.sources)
        valves = sorted(house.equipment.valves)
        output = args.csv.open("w", newline="", encoding="utf-8") if args.csv else nullcontext()
        with output as stream:
            writer = csv.writer(stream) if stream is not None else None
            if writer:
                writer.writerow([
                    "start_s", "end_s", "outdoor_c", "outdoor_measured_c",
                    "water_c", "return_c", "flow_l_min",
                    "electricity_kwh", "gas_kwh", "cost",
                    *(f"{name}_actual_c" for name in zones),
                    *(f"{name}_measured_c" for name in zones),
                    *(f"{name}_available" for name in sources),
                    *(f"{name}_request" for name in sources),
                    *(f"{name}_valve" for name in valves),
                ])
            for sample in scenario.run(controller):
                if writer:
                    writer.writerow([
                        sample.start_s, sample.end_s, sample.actual_outdoor_c,
                        sample.observation.outdoor_c, sample.water_c, sample.return_c,
                        sample.flow_l_min, sample.electricity_kwh,
                        sample.gas_kwh, sample.cost,
                        *(sample.actual_indoor_c[name] for name in zones),
                        *(sample.observation.indoor_c[name] for name in zones),
                        *(name in sample.observation.available_sources for name in sources),
                        *(sample.plan.sources.get(name, 0) for name in sources),
                        *(sample.plan.valves.get(name, 0) for name in valves),
                    ])
    except (OSError, ValueError, KeyError, TypeError) as error:
        parser.error(str(error))

    summary = {key: round(value, 4) for key, value in asdict(house.totals).items()}
    summary["final_indoor_c"] = {name: round(value, 3) for name, value in house.indoor_c.items()}
    summary["source_starts"] = house.source_starts
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()

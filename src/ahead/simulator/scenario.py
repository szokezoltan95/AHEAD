"""TOML scenarios, repeatable sensor faults, and experiment execution."""

from bisect import bisect_right
from dataclasses import dataclass
from math import isfinite
from pathlib import Path
from random import Random
import tomllib
from typing import Iterator

from ahead.control import Controller, ControlPlan, Observation
from ahead.simulator.house import Conditions, House, Partition, Source, WaterLoop, Zone


@dataclass(frozen=True)
class Fault:
    target: str
    start_s: int
    end_s: int
    bias_c: float | None = None

    def active(self, elapsed_s: int) -> bool:
        return self.start_s <= elapsed_s < self.end_s


@dataclass(frozen=True)
class Sample:
    start_s: int
    end_s: int
    observation: Observation
    plan: ControlPlan
    actual_indoor_c: dict[str, float]
    actual_outdoor_c: float
    water_c: float | None
    return_c: float | None
    flow_l_min: float
    electricity_kwh: float
    gas_kwh: float
    cost: float


@dataclass
class Scenario:
    house: House
    profile: tuple[tuple[int, Conditions], ...]
    step_s: int
    duration_s: int
    period_s: int | None
    faults: tuple[Fault, ...]
    sensor_noise_c: float
    seed: int

    @property
    def setpoints_c(self) -> dict[str, float]:
        return {name: zone.setpoint_c for name, zone in self.house.zones.items()}

    def run(self, controller: Controller) -> Iterator[Sample]:
        if self.house.elapsed_s:
            raise ValueError("Reload the scenario before running another experiment")
        random = Random(self.seed)
        times, weather = zip(*self.profile)
        for start_s in range(0, self.duration_s, self.step_s):
            profile_s = start_s if self.period_s is None else start_s % self.period_s
            conditions = weather[bisect_right(times, profile_s) - 1]
            faults = {fault.target: fault for fault in self.faults if fault.active(start_s)}

            def read_sensor(target, actual):
                fault = faults.get(target)
                if fault and fault.bias_c is None:
                    return None
                noise = random.gauss(0, self.sensor_noise_c) if self.sensor_noise_c else 0
                return actual + noise + (fault.bias_c if fault else 0)

            available = frozenset(
                name for name in self.house.sources if f"source:{name}" not in faults
            )
            observation = Observation(
                elapsed_s=start_s,
                indoor_c={
                    name: read_sensor(f"zone:{name}", value)
                    for name, value in self.house.indoor_c.items()
                },
                outdoor_c=read_sensor("outdoor", conditions.outdoor_c),
                available_sources=available,
                electricity_price_kwh=conditions.electricity_price_kwh,
                gas_price_kwh=conditions.gas_price_kwh,
            )
            plan = controller.decide(observation)
            end_s = min(self.duration_s, start_s + self.step_s)
            self.house.advance(end_s - start_s, conditions, plan, available)
            yield Sample(
                start_s, end_s, observation, plan, self.house.indoor_c,
                conditions.outdoor_c, self.house.water_c, self.house.return_c,
                self.house.flow_l_min, self.house.totals.electricity_kwh,
                self.house.totals.gas_kwh, self.house.totals.cost,
            )


def load_scenario(path: Path) -> Scenario:
    with path.open("rb") as file:
        data = tomllib.load(file)

    settings = data["simulation"]
    step_s = settings["step_s"]
    duration_s = settings["duration_s"]
    period_s = settings.get("profile_period_s")
    for name, value in (("step_s", step_s), ("duration_s", duration_s),
                        ("profile_period_s", period_s)):
        if name == "profile_period_s" and value is None:
            continue
        if type(value) is not int or value <= 0:
            raise ValueError(f"{name} must be a positive integer")
    if period_s and period_s % step_s:
        raise ValueError("Profile period must be aligned to the control step")

    house = House(
        tuple(Zone(**row) for row in data["zones"]),
        tuple(Partition(**row) for row in data.get("partitions", ())),
        tuple(Source(**row) for row in data.get("sources", ())),
        WaterLoop(**data["water_loop"]) if "water_loop" in data else None,
    )
    profile = []
    for row in data["profile"]:
        start = row["start_s"]
        if (type(start) is not int or start < 0 or start % step_s
                or (period_s is not None and start >= period_s)
                or (profile and start <= profile[-1][0])):
            raise ValueError("Profile starts must increase and align to the control step")
        profile.append((start, Conditions(**{k: v for k, v in row.items() if k != "start_s"})))
    if not profile or profile[0][0] != 0:
        raise ValueError("The profile must start at time zero")

    faults = tuple(Fault(**row) for row in data.get("faults", ()))
    targets = {"outdoor", *(f"zone:{name}" for name in house.zones),
               *(f"source:{name}" for name in house.sources)}
    last_end = {}
    for fault in sorted(faults, key=lambda item: item.start_s):
        if (fault.target not in targets or fault.start_s < last_end.get(fault.target, 0)
                or fault.start_s % step_s or fault.end_s % step_s
                or fault.end_s <= fault.start_s or fault.end_s > duration_s):
            raise ValueError("Faults must target known equipment and have aligned, separate intervals")
        if fault.bias_c is not None and (
            fault.target.startswith("source:") or not isfinite(fault.bias_c)
        ):
            raise ValueError("Only temperature sensors support finite fault bias")
        last_end[fault.target] = fault.end_s
    noise = settings.get("sensor_noise_c", 0.0)
    if not isfinite(noise) or noise < 0:
        raise ValueError("Sensor noise must be finite and non-negative")
    return Scenario(house, tuple(profile), step_s, duration_s, period_s,
                    faults, noise, settings.get("seed", 0))

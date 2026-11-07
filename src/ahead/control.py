"""Shared controller interface for simulation and Home Assistant."""

from dataclasses import dataclass
from math import isfinite
from typing import Mapping, Protocol


@dataclass(frozen=True)
class SourceCapabilities:
    """The zones a source can serve and its supported output control."""

    zones: frozenset[str]
    modulating: bool = False
    uses_water_loop: bool = False


@dataclass(frozen=True)
class Equipment:
    """Device capabilities and the zones with controllable radiator valves."""

    sources: Mapping[str, SourceCapabilities]
    valves: frozenset[str]


@dataclass(frozen=True)
class Observation:
    elapsed_s: float
    indoor_c: Mapping[str, float | None]
    outdoor_c: float | None
    available_sources: frozenset[str]
    electricity_price_kwh: float | None = None
    gas_price_kwh: float | None = None


@dataclass(frozen=True)
class ControlPlan:
    """Source and valve requests on a scale of zero to one."""

    sources: Mapping[str, float]
    valves: Mapping[str, float]


class Controller(Protocol):
    def decide(self, observation: Observation) -> ControlPlan: ...


class HysteresisController:
    """Baseline thermostat with ordered source preference and per-zone demand."""

    def __init__(
        self, equipment: Equipment, setpoints_c: Mapping[str, float], deadband_c: float
    ):
        if not isfinite(deadband_c) or deadband_c <= 0:
            raise ValueError("Deadband must be positive")
        if any(not isfinite(value) for value in setpoints_c.values()):
            raise ValueError("Setpoints must be finite")
        self._equipment = equipment
        self._setpoints = dict(setpoints_c)
        self._half_band = deadband_c / 2
        self._calling: set[str] = set()

    def decide(self, observation: Observation) -> ControlPlan:
        for zone, target in self._setpoints.items():
            temperature = observation.indoor_c.get(zone)
            if temperature is None or not isfinite(temperature):
                self._calling.discard(zone)
            elif temperature <= target - self._half_band:
                self._calling.add(zone)
            elif temperature >= target + self._half_band:
                self._calling.discard(zone)

        uncovered = self._calling.copy()
        directly_heated: set[str] = set()
        sources = dict.fromkeys(self._equipment.sources, 0.0)
        for name, capabilities in self._equipment.sources.items():
            if name in observation.available_sources and capabilities.zones & uncovered:
                sources[name] = 1.0
                uncovered.difference_update(capabilities.zones)
                if not capabilities.uses_water_loop:
                    directly_heated.update(capabilities.zones)
        return ControlPlan(
            sources=sources,
            valves={
                zone: float(zone in self._calling and zone not in directly_heated)
                for zone in self._equipment.valves
            },
        )

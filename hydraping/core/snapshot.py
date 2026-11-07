"""Immutable view of the engine state, built for one frame of a frontend."""

from dataclasses import dataclass

from hydraping.core.models import CheckResult, CheckType, Endpoint
from hydraping.core.store import Bucket


@dataclass(frozen=True)
class LayerView:
    """One layer of one endpoint: its latest result and the last N buckets, oldest first."""

    check_type: CheckType
    latest: CheckResult | None
    buckets: tuple[Bucket | None, ...]

    def results(self) -> list[CheckResult | None]:
        """Return the last result of each bucket, which is what the graph draws."""
        return [bucket.last if bucket is not None else None for bucket in self.buckets]


@dataclass(frozen=True)
class EndpointSnapshot:
    """One row of the display."""

    endpoint: Endpoint
    primary: CheckType
    # Read-only by convention: frontends never write into a snapshot.
    layers: dict[CheckType, LayerView]
    # The latest result of the primary layer; what the latency column shows.
    current: CheckResult | None
    # Problem lines after hierarchy suppression, without the endpoint name.
    problems: tuple[str, ...]

    def primary_layer(self) -> LayerView | None:
        return self.layers.get(self.primary)


@dataclass(frozen=True)
class EngineSnapshot:
    taken_at: float
    bucket_seconds: float
    columns: int
    rows: tuple[EndpointSnapshot, ...]
    icmp_available: bool

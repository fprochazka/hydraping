"""Immutable view of the engine state, built for one frame of a frontend."""

from dataclasses import dataclass

from hydraping.core.models import CheckResult, CheckType, Endpoint, LayerKey
from hydraping.core.store import Bucket


@dataclass(frozen=True)
class LayerView:
    """One layer of one endpoint: its latest result and the last N buckets, oldest first."""

    key: LayerKey
    latest: CheckResult | None
    buckets: tuple[Bucket | None, ...]

    @property
    def check_type(self) -> CheckType:
        return self.key.check_type

    def results(self) -> list[CheckResult | None]:
        """Return the last result of each bucket, which is what the graph draws."""
        return [bucket.last if bucket is not None else None for bucket in self.buckets]


@dataclass(frozen=True)
class EndpointSnapshot:
    """One row of the display."""

    endpoint: Endpoint
    primary: LayerKey
    # Read-only by convention: frontends never write into a snapshot.
    layers: dict[LayerKey, LayerView]
    # The latest result of the primary layer; what the latency column shows.
    current: CheckResult | None
    # Problem lines after hierarchy suppression, without the endpoint name.
    problems: tuple[str, ...]
    # The network interface the kernel routes the endpoint's address through, when known.
    interface: str | None = None

    def primary_layer(self) -> LayerView | None:
        return self.layers.get(self.primary)


@dataclass(frozen=True)
class ChainSnapshot:
    """A chain of dependent hops: a header row, with its hops below it."""

    name: str
    # Each hop as an endpoint row. Hops after the broken one have their problems suppressed.
    hops: tuple[EndpointSnapshot, ...]
    # Index of the first hop whose primary layer failed in the current bucket, or None.
    broken_at: int | None
    # The diagnosis line first, then the problems of the hops up to the broken one, each prefixed
    # with the hop's name. Without the chain's name.
    problems: tuple[str, ...]


@dataclass(frozen=True)
class EngineSnapshot:
    taken_at: float
    bucket_seconds: float
    columns: int
    # The standalone targets, in config order.
    rows: tuple[EndpointSnapshot, ...]
    icmp_available: bool
    chains: tuple[ChainSnapshot, ...] = ()

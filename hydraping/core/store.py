"""Time-bucketed result history, one ring of buckets per (endpoint, layer)."""

import math
import threading
from collections.abc import Sequence
from dataclasses import dataclass

from hydraping.core.models import CheckResult, CheckType

# Buckets kept per (endpoint, layer). At the default 5 s bucket this is five hours, enough for any
# terminal or window width. The size depends on time, not on the display.
HISTORY_BUCKETS = 3600


def bucket_index(finished_at: float, bucket_seconds: float) -> int:
    """Return the bucket a result finished in. Buckets are aligned to the epoch, not to startup."""
    return math.floor(finished_at / bucket_seconds)


@dataclass(frozen=True)
class Bucket:
    """One time bucket of a layer: the last result that landed in it, and how many did."""

    index: int
    last: CheckResult
    ok_count: int
    failed_count: int

    def with_result(self, result: CheckResult) -> "Bucket":
        return Bucket(
            index=self.index,
            last=result,
            ok_count=self.ok_count + (1 if result.success else 0),
            failed_count=self.failed_count + (0 if result.success else 1),
        )


class LayerHistory:
    """The buckets of one (endpoint, layer).

    ``apply`` enforces the ordering rule on its own, without trusting the scheduler: a result whose
    probe started before the probe of the last applied result is dropped. The scheduler cancels such
    probes too, but a probe that finished in the same event loop tick as its cancellation would
    otherwise still get through.
    """

    def __init__(self, bucket_seconds: float, capacity: int = HISTORY_BUCKETS):
        if bucket_seconds <= 0:
            raise ValueError("bucket_seconds must be positive")
        if capacity <= 0:
            raise ValueError("capacity must be positive")
        self.bucket_seconds = bucket_seconds
        self.capacity = capacity
        self._buckets: dict[int, Bucket] = {}
        self._newest_index: int | None = None
        self._latest: CheckResult | None = None

    @property
    def latest(self) -> CheckResult | None:
        """The last applied result; what "now" looks like for this layer."""
        return self._latest

    def apply(self, result: CheckResult) -> bool:
        """Record ``result`` and return whether it was kept."""
        if self._latest is not None and result.started_at < self._latest.started_at:
            return False
        self._latest = result

        index = bucket_index(result.finished_at, self.bucket_seconds)
        bucket = self._buckets.get(index)
        if bucket is None:
            bucket = Bucket(index=index, last=result, ok_count=0, failed_count=0)
        self._buckets[index] = bucket.with_result(result)

        if self._newest_index is None or index > self._newest_index:
            self._newest_index = index
            self._evict()
        return True

    def _evict(self) -> None:
        assert self._newest_index is not None
        oldest_kept = self._newest_index - self.capacity + 1
        if len(self._buckets) <= self.capacity and min(self._buckets) >= oldest_kept:
            return
        for index in [i for i in self._buckets if i < oldest_kept]:
            del self._buckets[index]

    def view(self, now: float, columns: int) -> tuple[Bucket | None, ...]:
        """Return the last ``columns`` buckets up to the one ``now`` falls in, oldest first.

        Buckets without a result are ``None``, including the ones that are still in the future
        of the newest result and the ones before the first.
        """
        current = bucket_index(now, self.bucket_seconds)
        first = current - columns + 1
        return tuple(self._buckets.get(index) for index in range(first, current + 1))


class EndpointHistory:
    """All layers of one endpoint."""

    def __init__(self, bucket_seconds: float, capacity: int = HISTORY_BUCKETS):
        self.bucket_seconds = bucket_seconds
        self.capacity = capacity
        self.layers: dict[CheckType, LayerHistory] = {}

    def layer(self, check_type: CheckType) -> LayerHistory:
        history = self.layers.get(check_type)
        if history is None:
            history = LayerHistory(self.bucket_seconds, self.capacity)
            self.layers[check_type] = history
        return history

    def apply(self, results: Sequence[CheckResult]) -> None:
        for result in results:
            self.layer(result.check_type).apply(result)


class BucketStore:
    """The histories of all endpoints behind one lock.

    The engine thread writes through ``apply``. Frontends read through ``Engine.snapshot()``, which
    holds ``lock`` for the whole read, so a snapshot never mixes two states.
    """

    def __init__(self, endpoint_count: int, bucket_seconds: float, capacity: int = HISTORY_BUCKETS):
        self.lock = threading.Lock()
        self.bucket_seconds = bucket_seconds
        self.endpoints = [EndpointHistory(bucket_seconds, capacity) for _ in range(endpoint_count)]

    def apply(self, endpoint_index: int, results: Sequence[CheckResult]) -> None:
        """Record the results of one probe for one endpoint. Thread-safe."""
        with self.lock:
            self.endpoints[endpoint_index].apply(results)

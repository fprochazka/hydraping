"""Tests for the bucket store, with explicit timestamps."""

import math

import pytest

from hydraping.core.models import CheckResult, CheckType, LayerKey
from hydraping.core.store import BucketStore, LayerHistory, bucket_index, history_capacity


def result(started_at: float, finished_at: float, latency_ms: float | None = 10.0) -> CheckResult:
    if latency_ms is None:
        return CheckResult(
            check_type=CheckType.ICMP,
            success=False,
            error_message="Timeout",
            started_at=started_at,
            finished_at=finished_at,
        )
    return CheckResult(
        check_type=CheckType.ICMP,
        success=True,
        latency_ms=latency_ms,
        started_at=started_at,
        finished_at=finished_at,
    )


class TestLayerHistory:
    def test_last_result_wins_within_a_bucket(self):
        history = LayerHistory(bucket_seconds=5.0)
        history.apply(result(100.0, 100.2, latency_ms=10.0))
        history.apply(result(101.0, 101.2, latency_ms=20.0))
        history.apply(result(102.0, 102.2, latency_ms=None))

        [bucket] = [b for b in history.view(now=104.9, columns=1) if b is not None]
        assert bucket.last.success is False
        assert (bucket.ok_count, bucket.failed_count) == (2, 1)
        assert history.latest is bucket.last

    def test_result_from_an_older_probe_is_dropped(self):
        history = LayerHistory(bucket_seconds=5.0)
        newer = result(101.0, 101.1, latency_ms=20.0)
        slow_older = result(100.0, 104.0, latency_ms=3000.0)

        assert history.apply(newer) is True
        assert history.apply(slow_older) is False

        assert history.latest is newer
        [bucket] = [b for b in history.view(now=104.9, columns=1) if b is not None]
        assert bucket.last is newer
        assert (bucket.ok_count, bucket.failed_count) == (1, 0)

    def test_equal_start_times_are_both_kept(self):
        # Two chains writing the same DNS layer (the TCP 80 and 443 chains of a domain) can start
        # in the same instant.
        history = LayerHistory(bucket_seconds=5.0)
        first = result(100.0, 100.1, latency_ms=1.0)
        second = result(100.0, 100.2, latency_ms=2.0)
        assert history.apply(first) and history.apply(second)
        assert history.latest is second

    def test_bucket_boundaries_follow_finished_at(self):
        history = LayerHistory(bucket_seconds=5.0)
        # Started in bucket 20 (100-104.99), finished in bucket 21.
        history.apply(result(104.0, 105.0))

        assert bucket_index(105.0, 5.0) == 21
        buckets = history.view(now=105.0, columns=2)
        assert buckets[0] is None
        assert buckets[1] is not None and buckets[1].index == 21

    def test_buckets_are_aligned_to_the_epoch(self):
        # Two histories started at different times share the same column boundaries.
        a = LayerHistory(bucket_seconds=5.0)
        b = LayerHistory(bucket_seconds=5.0)
        a.apply(result(100.0, 100.1))
        b.apply(result(103.0, 103.1))

        a_view = a.view(now=109.0, columns=2)
        b_view = b.view(now=109.0, columns=2)
        assert a_view[0] is not None and b_view[0] is not None
        assert a_view[0].index == b_view[0].index == 20

    def test_ring_drops_buckets_past_capacity(self):
        history = LayerHistory(bucket_seconds=1.0, capacity=3)
        for second in range(5):
            history.apply(result(float(second), second + 0.1))

        assert [b.index for b in history.view(now=4.5, columns=5) if b is not None] == [2, 3, 4]
        assert history.view(now=4.5, columns=5)[:2] == (None, None)

    @pytest.mark.parametrize(
        ("history_seconds", "bucket_seconds", "capacity"),
        [(3600, 5.0, 720), (3600, 3.0, 1200), (3600, 7.0, 515), (60, 5.0, 12), (5, 5.0, 1)],
    )
    def test_capacity_covers_the_history(self, history_seconds, bucket_seconds, capacity):
        assert history_capacity(history_seconds, bucket_seconds) == capacity
        store = BucketStore(1, bucket_seconds, history_seconds)
        assert store.capacity == capacity
        assert store.endpoints[0].layer(LayerKey(CheckType.TCP, 443)).capacity == capacity

    @pytest.mark.parametrize("bucket_seconds", [5.0, 3.0, 7.0])
    def test_default_history_drops_buckets_older_than_one_hour(self, bucket_seconds):
        history = LayerHistory(bucket_seconds=bucket_seconds)
        capacity = math.ceil(3600 / bucket_seconds)
        assert history.capacity == capacity
        # Two hours of results, one per bucket, starting at bucket 0.
        buckets = 2 * capacity
        for index in range(buckets):
            started = index * bucket_seconds
            history.apply(result(started, started + 0.1))

        now = (buckets - 1) * bucket_seconds + 0.5
        kept = [b for b in history.view(now=now, columns=buckets) if b is not None]
        assert len(kept) == capacity
        assert kept[0].index == buckets - capacity
        assert kept[-1].index == buckets - 1

    def test_view_pads_to_the_requested_columns(self):
        history = LayerHistory(bucket_seconds=5.0)
        history.apply(result(100.0, 100.1))

        view = history.view(now=100.5, columns=8)
        assert len(view) == 8
        assert view[:7] == (None,) * 7
        assert view[7] is not None

    def test_view_of_an_empty_history(self):
        assert LayerHistory(bucket_seconds=5.0).view(now=100.0, columns=4) == (None,) * 4

    def test_result_landing_in_an_older_bucket_after_a_clock_step(self):
        history = LayerHistory(bucket_seconds=5.0)
        history.apply(result(100.0, 106.0))
        # The clock stepped back: a later probe finishes with an earlier wall time.
        history.apply(result(101.0, 104.0, latency_ms=7.0))

        view = history.view(now=106.0, columns=2)
        assert view[0] is not None and view[0].last.latency_ms == 7.0
        assert view[1] is not None and view[1].index == 21


class TestBucketStore:
    def test_apply_creates_the_layer_on_first_result(self):
        store = BucketStore(endpoint_count=2, bucket_seconds=5.0)
        store.apply(1, [result(100.0, 100.1)])

        assert store.endpoints[0].layers == {}
        assert LayerKey(CheckType.ICMP) in store.endpoints[1].layers
        assert store.endpoints[1].layers[LayerKey(CheckType.ICMP)].latest is not None

    def test_tcp_results_are_kept_per_port(self):
        def tcp(port: int, started_at: float) -> CheckResult:
            return CheckResult(
                check_type=CheckType.TCP,
                success=True,
                latency_ms=1.0,
                port=port,
                started_at=started_at,
                finished_at=started_at + 0.1,
            )

        store = BucketStore(endpoint_count=1, bucket_seconds=5.0)
        store.apply(0, [tcp(80, 100.0)])
        store.apply(0, [tcp(443, 101.0)])

        layers = store.endpoints[0].layers
        assert set(layers) == {LayerKey(CheckType.TCP, 80), LayerKey(CheckType.TCP, 443)}
        assert layers[LayerKey(CheckType.TCP, 80)].latest.port == 80
        assert layers[LayerKey(CheckType.TCP, 443)].latest.port == 443

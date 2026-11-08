"""Tests for the adaptive probe interval: the mapping and the moving average."""

import pytest

from hydraping.core.interval import (
    FAST_SECONDS,
    SLOW_SECONDS,
    ProbeDurations,
    interval_for,
)

MIN, MAX = 1.0, 5.0


class TestIntervalFor:
    @pytest.mark.parametrize("average", [0.0, 0.05, FAST_SECONDS])
    def test_fast_gives_the_minimum(self, average):
        assert interval_for(average, MIN, MAX) == MIN

    @pytest.mark.parametrize("average", [SLOW_SECONDS, 0.75, 5.0, 60.0])
    def test_slow_gives_the_maximum(self, average):
        assert interval_for(average, MIN, MAX) == MAX

    def test_rises_linearly_in_between(self):
        assert interval_for(0.2, MIN, MAX) == pytest.approx(2.0)
        assert interval_for(0.3, MIN, MAX) == pytest.approx(3.0)
        assert interval_for(0.4, MIN, MAX) == pytest.approx(4.0)

    def test_just_past_the_fast_mark_is_just_above_the_minimum(self):
        assert MIN < interval_for(FAST_SECONDS + 0.001, MIN, MAX) < 1.02

    def test_equal_bounds_pin_the_interval(self):
        for average in (0.0, 0.3, 10.0):
            assert interval_for(average, MIN, MIN) == MIN

    def test_rejects_a_maximum_below_the_minimum(self):
        with pytest.raises(ValueError):
            interval_for(0.3, MIN, 0.5)


class TestProbeDurations:
    def test_starts_fast(self):
        assert ProbeDurations().average == 0.0
        assert interval_for(ProbeDurations().average, MIN, MAX) == MIN

    def test_fast_probes_keep_it_fast(self):
        durations = ProbeDurations()
        for _ in range(20):
            durations.record(0.02, success=True)
        assert durations.average < FAST_SECONDS

    def test_one_slow_probe_moves_it_halfway(self):
        durations = ProbeDurations()
        for _ in range(10):
            durations.record(0.01, success=True)
        average = durations.record(0.3, success=True)
        assert average == pytest.approx(0.155, abs=0.01)
        assert MIN < interval_for(average, MIN, MAX) < interval_for(0.3, MIN, MAX)

    def test_sustained_slow_probes_settle_near_the_duration(self):
        durations = ProbeDurations()
        for _ in range(10):
            durations.record(0.3, success=True)
        assert durations.average == pytest.approx(0.3, abs=0.001)

    def test_a_failure_counts_as_slow_whatever_it_took(self):
        quick_failure, long_failure = ProbeDurations(), ProbeDurations()
        assert quick_failure.record(0.001, success=False) == long_failure.record(5.0, success=False)
        assert quick_failure.average == SLOW_SECONDS / 2

    def test_a_long_success_counts_as_slow_and_no_more(self):
        capped = ProbeDurations()
        assert capped.record(10.0, success=True) == ProbeDurations().record(SLOW_SECONDS, True)

    def test_failures_approach_the_slow_mark(self):
        durations = ProbeDurations()
        averages = [durations.record(0.001, success=False) for _ in range(8)]
        assert averages == sorted(averages)
        assert averages[-1] > 0.49
        assert interval_for(averages[-1], MIN, MAX) > 4.9

    def test_three_fast_probes_bring_it_back_from_slow(self):
        durations = ProbeDurations()
        for _ in range(10):
            durations.record(0.0, success=False)
        for _ in range(3):
            durations.record(0.01, success=True)
        assert durations.average < FAST_SECONDS
        assert interval_for(durations.average, MIN, MAX) == MIN

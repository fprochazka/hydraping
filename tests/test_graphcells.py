"""Tests for the graph cell classification that every frontend draws from."""

import pytest

from hydraping.core.graphcells import (
    EMPTY_CELL,
    GAP_CELL,
    MAX_ERROR_GAP_SIZE,
    CellKind,
    GraphCell,
    LatencyColor,
    bar_level,
    classify_cells,
    latency_color,
)
from hydraping.core.models import CheckResult, CheckType


def ok(latency_ms: float) -> CheckResult:
    return CheckResult(
        check_type=CheckType.ICMP,
        success=True,
        latency_ms=latency_ms,
        started_at=1000.0,
        finished_at=1000.1,
    )


def failed(check_type: CheckType) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=False,
        error_message="x",
        started_at=1000.0,
        finished_at=1000.1,
    )


class TestBars:
    @pytest.mark.parametrize(
        ("latency_ms", "color", "level"),
        [
            (0.0, LatencyColor.GREEN, 0),
            (25.0, LatencyColor.GREEN, 3),
            (49.9, LatencyColor.GREEN, 6),
            (50.0, LatencyColor.YELLOW, 0),
            (99.9, LatencyColor.YELLOW, 6),
            (100.0, LatencyColor.ORANGE, 0),
            (200.0, LatencyColor.RED, 0),
            (500.0, LatencyColor.RED, 7),
            (5000.0, LatencyColor.RED, 7),
        ],
    )
    def test_colour_zone_and_level(self, latency_ms, color, level):
        assert latency_color(latency_ms) == color
        assert bar_level(latency_ms) == level
        assert classify_cells([ok(latency_ms)]) == [
            GraphCell(CellKind.BAR, color=color, level=level)
        ]


class TestFailures:
    @pytest.mark.parametrize(
        ("check_type", "letter"),
        [
            (CheckType.DNS, "D"),
            (CheckType.TCP, "T"),
            (CheckType.TLS, "S"),
            (CheckType.HTTP, "H"),
            (CheckType.ICMP, "!"),
            (CheckType.UDP, "!"),
            (CheckType.TAILSCALE, "!"),
        ],
    )
    def test_failure_letter(self, check_type, letter):
        assert classify_cells([failed(check_type)]) == [
            GraphCell(CellKind.FAILURE, color=LatencyColor.RED, letter=letter)
        ]


class TestGaps:
    def test_no_results_is_all_empty(self):
        assert classify_cells([None, None]) == [EMPTY_CELL, EMPTY_CELL]

    def test_empty_before_the_first_and_after_the_last_result(self):
        cells = classify_cells([None, ok(1.0), None])
        assert cells[0] == EMPTY_CELL and cells[2] == EMPTY_CELL
        assert cells[1].kind == CellKind.BAR

    def test_short_gap_between_results(self):
        results = [ok(1.0), *[None] * MAX_ERROR_GAP_SIZE, ok(1.0)]
        assert classify_cells(results)[1:-1] == [GAP_CELL] * MAX_ERROR_GAP_SIZE

    def test_long_gap_is_empty(self):
        results = [ok(1.0), *[None] * (MAX_ERROR_GAP_SIZE + 1), ok(1.0)]
        cells = classify_cells(results)
        assert cells[1:-1] == [EMPTY_CELL] * (MAX_ERROR_GAP_SIZE + 1)
        assert len(cells) == len(results)

"""What the top-bar item shows: rows, label, tooltip, icon pixels and the menu layout."""

from dbus_fast import Variant

from hydraping.core.graphcells import LatencyColor
from hydraping.core.models import CheckResult, CheckType, Endpoint
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot
from hydraping.gui import dbusmenu
from hydraping.gui.colors import LATENCY_RGB, NO_DATA_RGB
from hydraping.gui.tray_icon import icon_pixmaps, render_icon
from hydraping.gui.tray_status import (
    NO_DATA_LABEL,
    IconBar,
    RowState,
    bar_height,
    tray_rows,
    tray_status,
)

T = 1_000_000_000.0
GREEN = LATENCY_RGB[LatencyColor.GREEN]
YELLOW = LATENCY_RGB[LatencyColor.YELLOW]
RED = LATENCY_RGB[LatencyColor.RED]


def ok(check_type: CheckType, latency_ms: float, **fields) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=True,
        latency_ms=latency_ms,
        started_at=T - 0.01,
        finished_at=T,
        **fields,
    )


def failed(check_type: CheckType, message: str = "Timeout", **fields) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=False,
        error_message=message,
        started_at=T - 0.01,
        finished_at=T,
        **fields,
    )


def row(url: str, name: str, current: CheckResult | None, tray: bool = True) -> EndpointSnapshot:
    endpoint = Endpoint.parse(url)
    endpoint.custom_name = name
    endpoint.tray = tray
    return EndpointSnapshot(
        endpoint=endpoint,
        primary=endpoint.get_primary_layer(),
        layers={},
        current=current,
        problems=(),
    )


def chain(name: str, *hops: EndpointSnapshot, broken_at: int | None = None) -> ChainSnapshot:
    return ChainSnapshot(name=name, hops=hops, broken_at=broken_at, problems=())


def snapshot(rows=(), chains=()) -> EngineSnapshot:
    return EngineSnapshot(
        taken_at=T,
        bucket_seconds=5.0,
        columns=10,
        rows=tuple(rows),
        icmp_available=True,
        chains=tuple(chains),
    )


def router(latency_ms: float | None = 12.0, tray: bool = True) -> EndpointSnapshot:
    current = ok(CheckType.ICMP, latency_ms) if latency_ms is not None else None
    return row("192.0.2.1", "Router", current, tray=tray)


def jumpbox(current: CheckResult | None) -> EndpointSnapshot:
    return row("198.51.100.1:22", "Jumpbox", current)


def intranet(current: CheckResult | None) -> EndpointSnapshot:
    return row("https://intranet.example.internal/", "Intranet", current)


class TestRows:
    def test_chains_first_then_targets_shown_in_the_tray(self):
        hidden = row("203.0.113.5", "Wiki", ok(CheckType.ICMP, 5.0), tray=False)
        company = chain("Company", jumpbox(ok(CheckType.TCP, 20.0, port=22)))

        rows = tray_rows(snapshot(rows=[router(), hidden], chains=[company]))

        assert [r.name for r in rows] == ["Company", "Router"]

    def test_chain_latency_is_the_last_hop_and_its_bar_the_slowest_hop(self):
        company = chain(
            "Company",
            jumpbox(ok(CheckType.TCP, 120.0, port=22)),
            intranet(ok(CheckType.HTTP, 40.0)),
        )

        [company_row] = tray_rows(snapshot(chains=[company]))

        assert company_row.state == RowState.OK
        assert company_row.latency_ms == 40.0
        assert company_row.worst_latency_ms == 120.0

    def test_failed_target_is_down(self):
        failure = failed(CheckType.TCP, port=22)

        [jumpbox_row] = tray_rows(snapshot(rows=[jumpbox(failure)]))

        assert jumpbox_row.state == RowState.DOWN
        assert jumpbox_row.tooltip == "Jumpbox: FAIL (TCP)"


class TestLabel:
    def test_no_rows_gives_no_label(self):
        assert tray_status(snapshot(rows=[router(tray=False)])).label == ""

    def test_before_the_first_result(self):
        assert tray_status(snapshot(rows=[router(None)])).label == NO_DATA_LABEL

    def test_slowest_latency(self):
        slow = row("203.0.113.5", "Wiki", ok(CheckType.ICMP, 37.6))

        assert tray_status(snapshot(rows=[router(12.0), slow, router(None)])).label == "38 ms"

    def test_failing_targets_are_counted(self):
        down = [
            jumpbox(failed(CheckType.TCP, port=22)),
            row("203.0.113.5", "Wiki", failed(CheckType.ICMP)),
        ]

        assert tray_status(snapshot(rows=[router(), *down])).label == "2 down"

    def test_broken_chain_names_its_broken_hop(self):
        company = chain(
            "Company",
            jumpbox(failed(CheckType.TCP, port=22)),
            intranet(None),
            broken_at=0,
        )
        down = jumpbox(failed(CheckType.TCP, port=22))

        assert tray_status(snapshot(rows=[down], chains=[company])).label == "Jumpbox down"


class TestTooltip:
    def test_one_line_per_row(self):
        company = chain(
            "Company",
            jumpbox(ok(CheckType.TCP, 20.0, port=22)),
            intranet(ok(CheckType.HTTP, 45.1, protocol="https")),
        )
        broken = chain("Lab", jumpbox(failed(CheckType.TCP, port=22)), broken_at=0)

        status = tray_status(snapshot(rows=[router(12.0), router(None)], chains=[company, broken]))

        assert status.tooltip_title == "HydraPing"
        assert status.tooltip.splitlines() == [
            "Company: OK, 45.1ms",
            "Lab: broken at Jumpbox",
            "Router: 12.0ms (ICMP)",
            "Router: N/A",
        ]


class TestBars:
    def test_one_bar_per_row_coloured_by_latency(self):
        rows = [router(12.0), router(70.0), jumpbox(failed(CheckType.TCP, port=22)), router(None)]

        bars = tray_status(snapshot(rows=rows)).bars

        assert [bar.rgb for bar in bars] == [GREEN, YELLOW, RED, NO_DATA_RGB]
        assert bars[2].height == 1.0
        assert bars[0].height < bars[1].height < bars[2].height
        assert bars[3].height == bar_height(0.0)

    def test_at_most_five_bars(self):
        assert len(tray_status(snapshot(rows=[router()] * 7)).bars) == 5

    def test_slower_latency_never_gets_a_shorter_bar(self):
        heights = [bar_height(ms) for ms in range(0, 600, 5)]

        assert heights == sorted(heights)
        assert heights[-1] < 1.0

    def test_grey_bar_without_rows(self):
        assert tray_status(snapshot()).bars == (IconBar(NO_DATA_RGB, bar_height(0.0)),)


def pixel(image: bytes, size: int, x: int, y: int) -> bytes:
    start = (y * size + x) * 4
    return image[start : start + 4]


class TestIcon:
    def test_sizes_and_argb_in_network_byte_order(self):
        pixmaps = icon_pixmaps([IconBar((0x11, 0x22, 0x33), 1.0)])

        assert [(width, height, len(data)) for width, height, data in pixmaps] == [
            (22, 22, 22 * 22 * 4),
            (44, 44, 44 * 44 * 4),
        ]
        # A full bar covers the icon's centre: alpha first, then red, green and blue.
        assert pixel(pixmaps[0][2], 22, 11, 11) == bytes((0xFF, 0x11, 0x22, 0x33))
        # Outside the bars the icon is transparent.
        assert pixel(pixmaps[0][2], 22, 0, 0) == b"\x00\x00\x00\x00"

    def test_bars_stand_side_by_side_on_the_bottom(self):
        bars = [IconBar(GREEN, 0.3), IconBar(YELLOW, 0.5), IconBar(RED, 1.0)]
        size = 22

        image = render_icon(bars, size)

        bottom_row = [pixel(image, size, x, size - 2) for x in range(size)]
        colours = [bytes((0xFF, *bar.rgb)) for bar in bars]
        runs = []
        for value in bottom_row:
            if value != b"\x00\x00\x00\x00" and (not runs or runs[-1] != value):
                runs.append(value)
        assert runs == colours
        # The red bar reaches the top margin, the green one doesn't get near it.
        column_of = {value: x for x, value in enumerate(bottom_row)}
        assert pixel(image, size, column_of[colours[2]], 1) == colours[2]
        assert pixel(image, size, column_of[colours[0]], size // 2) == b"\x00\x00\x00\x00"


class TestMenuLayout:
    def make_menu(self, clicks: list[str]) -> dbusmenu.Menu:
        return dbusmenu.Menu(
            [
                dbusmenu.MenuItem(1, "Open HydraPing", lambda: clicks.append("open")),
                dbusmenu.MenuItem(2, "Quit", lambda: clicks.append("quit")),
            ]
        )

    def test_root_with_its_items(self):
        root_id, root_properties, children = self.make_menu([]).layout(0, -1, [])

        assert root_id == 0
        assert root_properties == {"children-display": Variant("s", "submenu")}
        assert all(child.signature == "(ia{sv}av)" for child in children)
        assert [(c.value[0], c.value[1]["label"].value, c.value[2]) for c in children] == [
            (1, "Open HydraPing", []),
            (2, "Quit", []),
        ]

    def test_depth_zero_and_property_filter(self):
        menu = self.make_menu([])

        assert menu.layout(0, 0, []) == [0, {"children-display": Variant("s", "submenu")}, []]
        _, root_properties, [_, quit_item] = menu.layout(0, 1, ["label"])
        assert root_properties == {}
        assert quit_item.value[1] == {"label": Variant("s", "Quit")}

    def test_group_properties_skip_unknown_ids(self):
        properties = self.make_menu([]).group_properties([2, 7], ["label", "enabled"])

        assert properties == [[2, {"label": Variant("s", "Quit"), "enabled": Variant("b", True)}]]

    def test_clicked_runs_the_item_action(self):
        clicks: list[str] = []
        menu = self.make_menu(clicks)

        assert menu.event(2, "clicked")
        assert menu.event(1, "hovered")
        assert not menu.event(7, "clicked")
        assert clicks == ["quit"]

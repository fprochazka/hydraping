"""Tests for the desktop window, on Qt's offscreen platform, from synthetic engine snapshots."""

import asyncio
import os
import socket
import sys
import time
from dataclasses import replace
from itertools import pairwise

import pytest
from dbus_fast import Variant
from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QSplitter, QWidget

from hydraping.core.config import ChecksConfig, Config, UIConfig
from hydraping.core.display import ICMP_UNAVAILABLE
from hydraping.core.graphcells import LatencyColor, classify_cells
from hydraping.core.models import CheckResult, CheckType, Endpoint, LayerKey, Measurement
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot, LayerView
from hydraping.core.store import Bucket
from hydraping.gui import dbusmenu
from hydraping.gui.app import GuiApp
from hydraping.gui.colors import LATENCY_RGB
from hydraping.gui.graph_widget import CELL_WIDTH, GraphWidget, bucket_tooltip
from hydraping.gui.window import INDENT, SCREEN_MARGIN, MainWindow
from tests.dbus_helpers import LoopThread, connect, disconnect, start_watcher

BUCKET = 5.0
# A bucket boundary; the snapshots below are taken in the bucket that starts here.
T = 1_000_000_000.0
COLUMNS = 6


@pytest.fixture(scope="module")
def qapp():
    """One QApplication on the offscreen platform, which needs no display."""
    previous = os.environ.get("QT_QPA_PLATFORM")
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    try:
        app = QApplication.instance() or QApplication(["hydraping-test"])
    finally:
        if previous is None:
            del os.environ["QT_QPA_PLATFORM"]
        else:
            os.environ["QT_QPA_PLATFORM"] = previous
    yield app


@pytest.fixture
def deadline(qapp):
    """Quit Qt's event loop after the given milliseconds, so a stuck test fails instead of hanging.

    The timer stops with the test; a bare ``QTimer.singleShot`` would outlive it and quit the
    event loop of a later test.
    """
    timers: list[QTimer] = []

    def start(milliseconds: int) -> None:
        timer = QTimer()
        timer.setSingleShot(True)
        timer.timeout.connect(qapp.quit)
        timer.start(milliseconds)
        timers.append(timer)

    yield start
    for timer in timers:
        timer.stop()


@pytest.fixture
def window(qapp):
    window = MainWindow()
    yield window
    window.close()
    window.deleteLater()


def ok(check_type: CheckType, at: float, latency_ms: float, **fields) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=True,
        latency_ms=latency_ms,
        started_at=at - 0.01,
        finished_at=at,
        **fields,
    )


def failed(check_type: CheckType, at: float, message: str, **fields) -> CheckResult:
    return CheckResult(
        check_type=check_type,
        success=False,
        error_message=message,
        started_at=at - 0.01,
        finished_at=at,
        **fields,
    )


def skipped(check_type: CheckType, blocked_by: CheckResult) -> CheckResult:
    return CheckResult.from_measurement(
        Measurement.skipped_after(check_type, blocked_by),
        started_at=blocked_by.started_at,
        finished_at=blocked_by.finished_at,
    )


def layer(*results: CheckResult) -> LayerView:
    """Return a view of COLUMNS buckets ending at the bucket of T, with one result per bucket."""
    by_index = {
        int(result.finished_at // BUCKET): Bucket(
            index=int(result.finished_at // BUCKET),
            last=result,
            ok_count=1 if result.success else 0,
            failed_count=0 if result.success else 1,
        )
        for result in results
    }
    current = int(T // BUCKET)
    buckets = tuple(by_index.get(index) for index in range(current - COLUMNS + 1, current + 1))
    return LayerView(key=results[-1].layer_key, latest=results[-1], buckets=buckets)


def row(
    url: str,
    name: str,
    layers: list[LayerView],
    problems: tuple[str, ...] = (),
    interface: str | None = None,
) -> EndpointSnapshot:
    endpoint = Endpoint.parse(url)
    endpoint.custom_name = name
    views = {view.key: view for view in layers}
    primary = endpoint.get_primary_layer()
    view = views.get(primary)
    return EndpointSnapshot(
        endpoint=endpoint,
        primary=primary,
        layers=views,
        current=view.latest if view is not None else None,
        problems=problems,
        interface=interface,
    )


def snapshot(router_latency_ms: float = 12.0) -> EngineSnapshot:
    tls_failure = failed(CheckType.TLS, T, "certificate verify failed")
    router = row(
        "192.0.2.1",
        "Router",
        [layer(ok(CheckType.ICMP, T - 10, 11.0), ok(CheckType.ICMP, T, router_latency_ms))],
    )
    intranet = row(
        "https://intranet.example.internal/health",
        "Intranet",
        [
            layer(ok(CheckType.DNS, T, 3.0)),
            layer(ok(CheckType.TCP, T, 9.0, port=443)),
            layer(tls_failure),
            layer(skipped(CheckType.HTTP, tls_failure)),
            layer(ok(CheckType.ICMP, T, 8.0)),
        ],
        problems=("TLS: certificate verify failed",),
        interface="eth0",
    )
    jumpbox = row("198.51.100.1:22", "Jumpbox", [layer(ok(CheckType.TCP, T, 20.0, port=22))])
    wiki = row(
        "203.0.113.5",
        "Wiki",
        [layer(failed(CheckType.ICMP, T, "Timeout"))],
    )
    chain = ChainSnapshot(
        name="Company",
        hops=(jumpbox, wiki),
        broken_at=1,
        problems=("broken at Wiki (ICMP: Timeout); Jumpbox OK",),
    )
    return EngineSnapshot(
        taken_at=T + 1,
        bucket_seconds=BUCKET,
        columns=COLUMNS,
        rows=(router, intranet),
        icmp_available=False,
        chains=(chain,),
    )


def snapshot_without_problems() -> EngineSnapshot:
    with_problems = snapshot()
    return replace(
        with_problems,
        rows=tuple(replace(row, problems=()) for row in with_problems.rows),
        chains=tuple(replace(chain, problems=()) for chain in with_problems.chains),
        icmp_available=True,
    )


def snapshot_with_many_rows(count: int) -> EngineSnapshot:
    rows = tuple(
        row(f"192.0.2.{index}", f"Host {index}", [layer(ok(CheckType.ICMP, T, 10.0))])
        for index in range(1, count + 1)
    )
    return replace(snapshot_without_problems(), rows=rows, chains=())


def snapshot_with_a_long_problem() -> EngineSnapshot:
    with_problems = snapshot()
    long_problem = "TLS: " + " ".join(["certificate verify failed"] * 12)
    intranet = replace(with_problems.rows[1], problems=(long_problem,))
    return replace(with_problems, rows=(with_problems.rows[0], intranet))


def assert_height_fits_content(window: MainWindow) -> None:
    """The window ends right below its last visible row or problem line, without a scroll bar."""
    QApplication.processEvents()
    assert not window._rows_scroll.verticalScrollBar().isVisible()
    content = window._rows_widget
    visible = [child for child in content.findChildren(QWidget) if child.isVisible()]
    bottom = max(child.mapTo(window, QPoint(0, child.height())).y() for child in visible)
    bottom_margin = content.layout().contentsMargins().bottom()
    assert bottom <= window.height() <= bottom + bottom_margin + 4
    assert abs(window.height() - window.content_height(window.width())) <= 4


class TestMainWindow:
    def test_rows_show_names_latency_labels_and_routes(self, window):
        window.show_snapshot(snapshot())

        headers = [endpoint_row.header for endpoint_row in window.endpoint_rows]
        assert [header.name.text() for header in headers] == [
            "Router",
            "Intranet",
            "Jumpbox",
            "Wiki",
        ]
        assert [header.latency.text() for header in headers] == ["12.0ms", "FAIL", "20.0ms", "FAIL"]
        assert [header.label.text() for header in headers] == [
            "(ICMP)",
            "(TLS)",
            "(TCP:22)",
            "(ICMP)",
        ]
        assert [header.route.text() for header in headers] == ["", "via eth0", "", ""]

    def test_chain_header_and_indented_hops(self, window):
        window.show_snapshot(snapshot())

        [header] = window.chain_headers
        assert header.name.text() == "Company"
        assert header.status.text() == "broken at Wiki"
        indents = [endpoint_row.header.name.indent() for endpoint_row in window.endpoint_rows]
        assert indents == [0, 0, INDENT, INDENT]

    def test_rows_and_problems_share_one_scroll_area_without_a_splitter(self, window):
        window.show_snapshot(snapshot())

        assert window.findChildren(QSplitter) == []

    def test_problems_are_listed_right_below_the_last_row(self, window):
        window.show_snapshot(snapshot())
        window.show()
        QApplication.processEvents()

        assert window.problems_section.isVisible()
        assert window.problems.text().splitlines() == [
            f"• {ICMP_UNAVAILABLE}",
            "• Intranet: TLS: certificate verify failed",
            "• Company: broken at Wiki (ICMP: Timeout); Jumpbox OK",
        ]
        last_row = window.endpoint_rows[-1]
        last_row_bottom = last_row.mapTo(window, QPoint(0, last_row.height())).y()
        problems_top = window.problems_section.mapTo(window, QPoint(0, 0)).y()
        assert last_row_bottom <= problems_top < last_row_bottom + 40

    def test_problems_are_hidden_while_there_are_none(self, window):
        window.show_snapshot(snapshot_without_problems())
        window.show()

        assert not window.problems_section.isVisible()

    def test_problems_coming_and_going_keep_the_rows_and_their_expanded_state(self, window):
        window.show_snapshot(snapshot())
        window.show()
        intranet = window.endpoint_rows[1]
        intranet.toggle_expanded()

        window.show_snapshot(snapshot_without_problems())
        assert not window.problems_section.isVisible()
        window.show_snapshot(snapshot())

        assert window.problems_section.isVisible()
        assert window.endpoint_rows[1] is intranet
        assert intranet.is_expanded()

    def test_graph_paints_the_shared_cells_of_the_primary_layer(self, window):
        window.show_snapshot(snapshot())

        router = window.endpoint_rows[0]
        primary = snapshot().rows[0].primary_layer()
        assert primary is not None
        assert router.graph.cells() == classify_cells(primary.results())

    def test_click_expands_one_graph_per_layer_in_probe_order(self, window):
        window.show_snapshot(snapshot())
        window.show()
        intranet = window.endpoint_rows[1]
        assert not intranet.is_expanded()

        QTest.mouseClick(intranet.header, Qt.MouseButton.LeftButton)

        assert intranet.is_expanded()
        assert intranet.details.isVisible()
        assert intranet.layer_names() == ["ICMP", "DNS", "TCP:443", "TLS", "HTTP"]
        tls = intranet.layer_rows[LayerKey(CheckType.TLS)]
        assert tls.latency.text() == "FAIL"
        assert tls.latency.toolTip() == "certificate verify failed"
        http = intranet.layer_rows[LayerKey(CheckType.HTTP)]
        # A skipped layer names the layer that blocked it.
        assert (http.latency.text(), http.label.text()) == ("FAIL", "(TLS)")

        QTest.mouseClick(intranet.header, Qt.MouseButton.LeftButton)
        assert not intranet.is_expanded()

    def test_click_on_a_label_or_the_graph_toggles_the_row(self, window):
        window.show_snapshot(snapshot())
        window.show()
        hop = window.endpoint_rows[3]
        middle = hop.header.height() // 2

        # The right edge is the route column.
        QTest.mouseClick(
            hop.header, Qt.MouseButton.LeftButton, pos=QPoint(hop.header.width() - 2, middle)
        )
        assert hop.is_expanded()

        QTest.mouseClick(
            hop.graph, Qt.MouseButton.LeftButton, pos=QPoint(hop.graph.width() // 2, middle)
        )
        assert not hop.is_expanded()

    def test_next_snapshot_updates_the_same_rows_and_keeps_them_expanded(self, window):
        window.show_snapshot(snapshot())
        router = window.endpoint_rows[0]
        router.toggle_expanded()

        window.show_snapshot(snapshot(router_latency_ms=150.0))

        assert window.endpoint_rows[0] is router
        assert router.is_expanded()
        assert router.header.latency.text() == "150.0ms"
        assert router.layer_rows[LayerKey(CheckType.ICMP)].latency.text() == "150.0ms"

    def test_refreshing_keeps_the_reference_count_of_none(self, window):
        # PySide6 6.12.0 drops a reference to None on every call that returns nothing, and Python
        # 3.11 aborts when the count reaches zero. This catches a version with that bug.
        # The warm-up fills Python's and Qt's caches first; even then, normal noise moves the count
        # by about a hundred. With four rows and at least one such call per row, 500 refreshes drop
        # 2000 or more references under the bug, well past the 1000 the check allows for noise.
        for _ in range(10):
            window.show_snapshot(snapshot())
        before = sys.getrefcount(None)
        for _ in range(500):
            window.show_snapshot(snapshot())
        assert sys.getrefcount(None) >= before - 1000

    def test_graph_columns_follow_the_window_width(self, window):
        window.resize(600, 400)
        window.show()
        narrow = window.graph_columns()
        window.resize(1200, 400)
        QApplication.processEvents()

        assert window.graph_columns() > narrow > 0

    def test_window_height_fits_its_content(self, window):
        window.show_snapshot(snapshot())
        window.show()

        assert_height_fits_content(window)

    def test_only_the_width_can_be_dragged(self, window):
        window.show_snapshot(snapshot())
        window.show()

        assert window.minimumHeight() == window.maximumHeight() == window.height()
        assert window.minimumWidth() < window.width() < window.maximumWidth()

    def test_expanding_a_row_grows_the_window_and_collapsing_shrinks_it(self, window):
        window.show_snapshot(snapshot())
        window.show()
        collapsed = window.height()
        intranet = window.endpoint_rows[1]

        QTest.mouseClick(intranet.header, Qt.MouseButton.LeftButton)
        assert window.height() > collapsed
        assert_height_fits_content(window)

        QTest.mouseClick(intranet.header, Qt.MouseButton.LeftButton)
        assert window.height() == collapsed
        assert_height_fits_content(window)

    def test_problems_appearing_grow_the_window_and_disappearing_shrink_it(self, window):
        window.show_snapshot(snapshot_without_problems())
        window.show()
        without = window.height()
        assert_height_fits_content(window)

        window.show_snapshot(snapshot())
        assert window.height() > without
        assert_height_fits_content(window)

        window.show_snapshot(snapshot_without_problems())
        assert window.height() == without
        assert_height_fits_content(window)

    def test_a_wider_window_fits_the_rewrapped_problems(self, window):
        window.show_snapshot(snapshot_with_a_long_problem())
        window.show()
        window.resize(window.minimumWidth(), window.height())
        QApplication.processEvents()
        narrow = window.height()
        assert_height_fits_content(window)

        window.resize(1600, window.height())
        QApplication.processEvents()

        assert window.width() == 1600
        assert window.height() < narrow
        assert_height_fits_content(window)

    def test_many_rows_cap_the_height_at_the_screen_and_scroll(self, window):
        window.show_snapshot(snapshot_with_many_rows(60))
        window.show()
        QApplication.processEvents()

        available = window.screen().availableGeometry().height()
        assert window.content_height(window.width()) > available
        assert window.height() == window.height_cap() == available - SCREEN_MARGIN
        assert window.minimumHeight() == window.maximumHeight()
        assert window._rows_scroll.verticalScrollBar().isVisible()

    def test_a_hidden_window_fits_its_content_when_shown(self, window):
        window.show_snapshot(snapshot())
        window.endpoint_rows[1].toggle_expanded()

        window.show()

        assert_height_fits_content(window)


class TestGraphWidget:
    WIDTH = CELL_WIDTH * 12

    def test_newest_bucket_is_painted_at_the_right_edge(self, qapp):
        graph = GraphWidget(height=24)
        graph.resize(self.WIDTH, 24)
        view = layer(ok(CheckType.ICMP, T - 5, 10.0), failed(CheckType.ICMP, T, "Timeout"))
        graph.set_layer(view, BUCKET)

        image = graph.grab().toImage()

        def rgb(x: int, y: int) -> tuple[int, int, int]:
            color = image.pixelColor(x, y)
            return color.red(), color.green(), color.blue()

        last_column = self.WIDTH - CELL_WIDTH + 1
        assert rgb(last_column, 1) == LATENCY_RGB[LatencyColor.RED]
        assert rgb(last_column - CELL_WIDTH, 23) == LATENCY_RGB[LatencyColor.GREEN]
        # A 10 ms bar is low, so the top of its column stays unpainted.
        assert rgb(last_column - CELL_WIDTH, 1) != LATENCY_RGB[LatencyColor.GREEN]

    def test_columns_map_to_buckets_from_the_right(self, qapp):
        graph = GraphWidget()
        graph.resize(self.WIDTH, 24)
        newest = ok(CheckType.ICMP, T, 10.0)
        graph.set_layer(layer(newest), BUCKET)

        over, bucket = graph.bucket_at(self.WIDTH - 2)
        assert over and bucket is not None and bucket.last is newest
        assert graph.bucket_at(self.WIDTH - CELL_WIDTH - 2) == (True, None)
        # The snapshot has COLUMNS buckets; the columns left of them have none.
        assert graph.bucket_at(self.WIDTH - CELL_WIDTH * COLUMNS - 2) == (False, None)


class TestBucketTooltip:
    def test_failure_shows_the_error_and_the_counts(self):
        result = failed(CheckType.TCP, T + 1, "Connection refused", port=443)
        bucket = Bucket(index=int(T // BUCKET), last=result, ok_count=2, failed_count=1)

        lines = bucket_tooltip(bucket, BUCKET).splitlines()

        assert lines[0].endswith("FAIL (TCP)")
        assert lines[1:] == ["Connection refused", "2 OK, 1 failed in this bucket"]

    def test_missing_bucket(self):
        assert bucket_tooltip(None, BUCKET) == "No result"


class TestGuiApp:
    def test_without_a_top_bar_host_closing_the_window_quits(self, qapp, deadline):
        # The tests' session bus doesn't exist (see conftest), so no host shows the item.
        # A listening socket accepts TCP connections in the kernel, without an accept() loop.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(128)
            port = server.getsockname()[1]
            config = Config(
                endpoints=[Endpoint.parse(f"127.0.0.1:{port}")],
                checks=ChecksConfig(probe_interval_seconds=0.1, timeout_seconds=1.0),
            )
            gui = GuiApp(config, qapp)
            seen: list[str] = []

            def close_once_measured():
                rows = gui.window.endpoint_rows
                if rows and rows[0].header.latency.text().endswith("ms"):
                    seen.append(rows[0].header.label.text())
                    gui.window.close()

            poll = QTimer()
            poll.setInterval(50)
            poll.timeout.connect(close_once_measured)
            poll.start()
            deadline(10_000)
            try:
                exit_code = gui.run()
            finally:
                poll.stop()

        assert exit_code == 0
        assert seen == [f"(TCP:{port})"]
        assert gui.engine._thread is None
        assert not gui.tray.registered

    def test_with_a_top_bar_host_closing_hides_and_quit_in_the_menu_quits(
        self, qapp, private_bus, deadline
    ):
        loop = LoopThread()
        watcher_bus, watcher = loop.run(start_watcher(private_bus))
        client = loop.run(connect(private_bus))
        config = Config(
            endpoints=[Endpoint.parse("192.0.2.1")],
            checks=ChecksConfig(probe_interval_seconds=0.1, timeout_seconds=1.0),
        )
        gui = GuiApp(config, qapp, bus_address=private_bus)
        steps: list[str] = []

        async def click_quit():
            node = await client.introspect(gui.tray.unique_name, dbusmenu.OBJECT_PATH)
            menu = client.get_proxy_object(
                gui.tray.unique_name, dbusmenu.OBJECT_PATH, node
            ).get_interface(dbusmenu.INTERFACE)
            await menu.call_event(2, "clicked", Variant("s", ""), 0)

        def step():
            if not steps and gui.window.isVisible() and gui.window.hide_on_close:
                gui.window.close()
                steps.append("closed")
            elif steps == ["closed"]:
                steps.append("hidden" if gui.window.isHidden() else "visible")
                # Quit arrives from the D-Bus thread; don't block Qt's thread on it.
                asyncio.run_coroutine_threadsafe(click_quit(), loop.loop)

        poll = QTimer()
        poll.setInterval(50)
        poll.timeout.connect(step)
        poll.start()
        deadline(10_000)
        try:
            exit_code = gui.run()
        finally:
            poll.stop()
            loop.run(disconnect(client))
            loop.run(disconnect(watcher_bus))
            loop.close()

        assert exit_code == 0
        assert steps == ["closed", "hidden"]
        assert watcher.items == [gui.tray.unique_name]
        assert gui.engine._thread is None

    def test_top_bar_item_updates_at_most_every_tray_refresh_seconds(
        self, qapp, private_bus, deadline
    ):
        tray_refresh_seconds = 0.5
        loop = LoopThread()
        watcher_bus, watcher = loop.run(start_watcher(private_bus))
        # A listening socket accepts TCP connections in the kernel, so every probe of it succeeds
        # with a slightly different latency, and every window refresh has a new snapshot.
        with socket.socket() as server:
            server.bind(("127.0.0.1", 0))
            server.listen(128)
            config = Config(
                endpoints=[Endpoint.parse(f"127.0.0.1:{server.getsockname()[1]}")],
                checks=ChecksConfig(probe_interval_seconds=0.05, timeout_seconds=1.0),
                ui=UIConfig(tray_refresh_seconds=tray_refresh_seconds),
            )
            gui = GuiApp(config, qapp, bus_address=private_bus)
            gui.timer.setInterval(50)
            refreshed_at: list[float] = []
            tray_updated_at: list[float] = []
            show_snapshot, update = gui.window.show_snapshot, gui.tray.update

            def recording_show_snapshot(snapshot):
                refreshed_at.append(time.monotonic())
                show_snapshot(snapshot)

            def recording_update(status):
                tray_updated_at.append(time.monotonic())
                update(status)

            gui.window.show_snapshot = recording_show_snapshot
            gui.tray.update = recording_update
            stop = QTimer()
            stop.setSingleShot(True)
            stop.timeout.connect(gui.quit)
            stop.start(2_000)
            deadline(10_000)
            started = time.monotonic()
            try:
                exit_code = gui.run()
            finally:
                stop.stop()
                loop.run(disconnect(watcher_bus))
                loop.close()
            elapsed = time.monotonic() - started

        assert exit_code == 0
        assert watcher.items == [gui.tray.unique_name]
        # The window kept refreshing every 50 ms, the top-bar item only every 0.5 s.
        assert len(refreshed_at) >= 15
        assert tray_updated_at[0] - started < 0.2, "the first update must not wait"
        gaps = [later - earlier for earlier, later in pairwise(tray_updated_at)]
        assert min(gaps) >= tray_refresh_seconds - 0.01
        assert 3 <= len(tray_updated_at) <= elapsed / tray_refresh_seconds + 1

"""The main window: one row per target and per chain hop, and the problems right below them."""

from dataclasses import dataclass

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontMetrics, QMouseEvent, QResizeEvent, QShowEvent
from PySide6.QtWidgets import (
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QScrollArea,
    QVBoxLayout,
    QWidget,
)

from hydraping.core.display import (
    ChainState,
    LatencyText,
    chain_status,
    latency_text,
    problem_lines,
    route_label,
)
from hydraping.core.graphcells import LatencyColor
from hydraping.core.models import CheckType, LayerKey
from hydraping.core.snapshot import ChainSnapshot, EndpointSnapshot, EngineSnapshot, LayerView
from hydraping.core.store import HISTORY_BUCKETS
from hydraping.gui.colors import LATENCY_RGB, NO_DATA_RGB, hex_color
from hydraping.gui.graph_widget import CELL_WIDTH, GRAPH_HEIGHT, LAYER_GRAPH_HEIGHT, GraphWidget

WINDOW_TITLE = "HydraPing"
# The width the window opens at; its height always follows its content.
DEFAULT_WIDTH = 960
# Room left on the screen for the window's title bar and frame when the content is taller.
SCREEN_MARGIN = 64
# Qt's largest widget size, the cap while the window is on no screen yet.
QWIDGETSIZE_MAX = (1 << 24) - 1

# Hops sit indented under their chain's header, and layers under their row.
INDENT = 16
MAX_NAME_WIDTH = 280
COLUMN_PADDING = 8
EXPANDED_MARK = "▾"
COLLAPSED_MARK = "▸"

# The order of the layer rows when a row is expanded: the order a probe runs them in.
LAYER_ORDER = (
    CheckType.ICMP,
    CheckType.DNS,
    CheckType.TCP,
    CheckType.UDP,
    CheckType.TLS,
    CheckType.HTTP,
    CheckType.TAILSCALE,
)


def layer_sort_key(key: LayerKey) -> tuple[int, int]:
    return LAYER_ORDER.index(key.check_type), key.port or 0


def _color_style(color: LatencyColor | None) -> str:
    rgb = LATENCY_RGB[color] if color is not None else NO_DATA_RGB
    return f"color: {hex_color(rgb)};"


@dataclass(frozen=True)
class ColumnWidths:
    """Pixel widths of the fixed columns, shared by every row so the graphs line up."""

    mark: int
    name: int
    latency: int
    label: int
    route: int


class _Columns(QWidget):
    """A row of the fixed columns around a graph: mark, name, graph, latency, label and route."""

    def __init__(self, graph: QWidget, parent: QWidget | None = None):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(COLUMN_PADDING)
        self.mark = QLabel()
        self.name = QLabel()
        self.graph = graph
        self.latency = QLabel()
        self.latency.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        self.label = QLabel()
        self.route = QLabel()
        for label in (self.label, self.route):
            label.setStyleSheet(_color_style(None))
        layout.addWidget(self.mark)
        layout.addWidget(self.name)
        layout.addWidget(self.graph, 1)
        layout.addWidget(self.latency)
        layout.addWidget(self.label)
        layout.addWidget(self.route)

    def apply_widths(self, widths: ColumnWidths) -> None:
        self.mark.setFixedWidth(widths.mark)
        self.name.setFixedWidth(widths.name)
        self.latency.setFixedWidth(widths.latency)
        self.label.setFixedWidth(widths.label)
        self.route.setFixedWidth(widths.route)
        self.route.setVisible(widths.route > 0)

    def show_latency(self, latency: LatencyText) -> None:
        self.latency.setText(latency.text)
        self.latency.setStyleSheet(_color_style(latency.color))
        self.label.setText(latency.label)


class _ClickableRow(_Columns):
    clicked = Signal()

    def __init__(self, graph: QWidget, parent: QWidget | None = None):
        super().__init__(graph, parent)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        # Accepting the press makes this row receive the release, also when the press landed on
        # one of its labels or on the graph, which don't take mouse presses themselves.
        if event.button() == Qt.MouseButton.LeftButton:
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        inside = self.rect().contains(event.position().toPoint())
        if event.button() == Qt.MouseButton.LeftButton and inside:
            self.clicked.emit()
        super().mouseReleaseEvent(event)


class LayerRow(_Columns):
    """One layer of an expanded row: its own small graph and latest result."""

    def __init__(self, key: LayerKey, indent: int, parent: QWidget | None = None):
        super().__init__(GraphWidget(LAYER_GRAPH_HEIGHT), parent)
        self.key = key
        self.name.setText(key.label)
        self.name.setIndent(indent)
        self.name.setStyleSheet(_color_style(None))

    def show_layer(self, view: LayerView, bucket_seconds: float) -> None:
        assert isinstance(self.graph, GraphWidget)
        self.graph.set_layer(view, bucket_seconds)
        self.show_latency(latency_text(view.latest))
        latest = view.latest
        error = latest.error_message if latest is not None else None
        self.latency.setToolTip(error or "")


class EndpointRow(QFrame):
    """A target or a chain hop. A click expands a small graph for each of its layers."""

    expanded_changed = Signal()

    def __init__(self, indent: int = 0, parent: QWidget | None = None):
        super().__init__(parent)
        self.indent = indent
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.header = _ClickableRow(GraphWidget())
        self.header.name.setIndent(indent)
        bold = self.header.name.font()
        bold.setBold(True)
        self.header.name.setFont(bold)
        self.header.clicked.connect(self.toggle_expanded)
        layout.addWidget(self.header)
        self.details = QWidget()
        self._details_layout = QVBoxLayout(self.details)
        self._details_layout.setContentsMargins(0, 0, 0, 4)
        self._details_layout.setSpacing(2)
        self.details.setVisible(False)
        layout.addWidget(self.details)
        self.layer_rows: dict[LayerKey, LayerRow] = {}
        self._widths: ColumnWidths | None = None
        self._update_mark()

    @property
    def graph(self) -> GraphWidget:
        assert isinstance(self.header.graph, GraphWidget)
        return self.header.graph

    def is_expanded(self) -> bool:
        return not self.details.isHidden()

    def toggle_expanded(self) -> None:
        self.details.setVisible(not self.is_expanded())
        self._update_mark()
        # Hiding the details alone updates the parent's layout only on the next event loop pass;
        # the window reads the new height right away.
        self.updateGeometry()
        self.expanded_changed.emit()

    def _update_mark(self) -> None:
        self.header.mark.setText(EXPANDED_MARK if self.is_expanded() else COLLAPSED_MARK)

    def apply_widths(self, widths: ColumnWidths) -> None:
        self._widths = widths
        self.header.apply_widths(widths)
        for layer_row in self.layer_rows.values():
            layer_row.apply_widths(widths)

    def show_row(self, row: EndpointSnapshot, bucket_seconds: float) -> None:
        self.header.name.setText(row.endpoint.display_name)
        self.header.name.setToolTip(row.endpoint.raw)
        self.graph.set_layer(row.primary_layer(), bucket_seconds)
        self.header.show_latency(latency_text(row.current))
        self.header.route.setText(route_label(row))
        self._show_layers(row, bucket_seconds)

    def _show_layers(self, row: EndpointSnapshot, bucket_seconds: float) -> None:
        keys = sorted(row.layers, key=layer_sort_key)
        if keys != list(self.layer_rows):
            # Layers appear as their first results arrive; rebuild the list in probe order.
            for layer_row in self.layer_rows.values():
                self._details_layout.removeWidget(layer_row)
                layer_row.deleteLater()
            self.layer_rows = {}
            for key in keys:
                layer_row = LayerRow(key, self.indent + INDENT)
                if self._widths is not None:
                    layer_row.apply_widths(self._widths)
                self._details_layout.addWidget(layer_row)
                self.layer_rows[key] = layer_row
        for key in keys:
            self.layer_rows[key].show_layer(row.layers[key], bucket_seconds)

    def layer_names(self) -> list[str]:
        return [layer_row.name.text() for layer_row in self.layer_rows.values()]


class ChainHeader(_Columns):
    """A chain's header row: its name, and where the graphs are, whether and where it broke."""

    def __init__(self, parent: QWidget | None = None):
        status = QLabel()
        super().__init__(status, parent)
        self.status = status
        font = self.name.font()
        font.setBold(True)
        font.setUnderline(True)
        self.name.setFont(font)

    def show_chain(self, chain: ChainSnapshot) -> None:
        self.name.setText(chain.name)
        status = chain_status(chain)
        self.status.setText(status.text)
        color = {ChainState.BROKEN: LatencyColor.RED, ChainState.OK: LatencyColor.GREEN}.get(
            status.state
        )
        self.status.setStyleSheet(_color_style(color))


def _structure(snapshot: EngineSnapshot) -> tuple:
    """Return what decides the window's rows. The rows are rebuilt only when it changes."""
    return (
        tuple(row.endpoint.display_name for row in snapshot.rows),
        tuple(
            (chain.name, tuple(hop.endpoint.display_name for hop in chain.hops))
            for chain in snapshot.chains
        ),
    )


class MainWindow(QMainWindow):
    """Show engine snapshots: the same rows as the terminal UI, with painted graphs.

    The window never reads the engine. Its owner passes each snapshot to ``show_snapshot``.
    Its height follows the content, up to the screen's height, and only its width can be dragged.
    """

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setWindowTitle(WINDOW_TITLE)
        self.resize(DEFAULT_WIDTH, self.height())
        self._fitted_height: int | None = None
        self._screen_connected = False

        problems_heading = QLabel("Problems:")
        problems_heading.setStyleSheet(f"{_color_style(LatencyColor.RED)} font-weight: bold;")
        self.problems = QLabel()
        self.problems.setWordWrap(True)
        self.problems.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.problems.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.problems.setStyleSheet(_color_style(LatencyColor.RED))
        self.problems_section = QWidget()
        problems_layout = QVBoxLayout(self.problems_section)
        problems_layout.setContentsMargins(0, 12, 0, 0)
        problems_layout.setSpacing(4)
        problems_layout.addWidget(problems_heading)
        problems_layout.addWidget(self.problems)

        # The rows go above the problems, and a stretch below both keeps them at the top.
        self._rows_widget = QWidget()
        self._rows_layout = QVBoxLayout(self._rows_widget)
        self._rows_layout.setSpacing(4)
        self._rows_layout.addWidget(self.problems_section)
        self._rows_layout.addStretch(1)
        self._rows_scroll = QScrollArea()
        self._rows_scroll.setWidgetResizable(True)
        self._rows_scroll.setFrameShape(QFrame.Shape.NoFrame)
        self._rows_scroll.setWidget(self._rows_widget)
        self._rows_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setCentralWidget(self._rows_scroll)

        self.endpoint_rows: list[EndpointRow] = []
        self.chain_headers: list[ChainHeader] = []
        self._structure: tuple | None = None
        self._show_problems([])
        self._update_minimum_width()
        self.fit_height()

    def content_height(self, width: int) -> int:
        """Return the height the rows and the problems need at the given window width."""
        height = self._rows_widget.heightForWidth(width)
        return height if height >= 0 else self._rows_widget.sizeHint().height()

    def height_cap(self) -> int:
        """Return the tallest the window gets: the free height of its screen, less a margin."""
        screen = self.screen()
        if screen is None:
            return QWIDGETSIZE_MAX
        return max(GRAPH_HEIGHT, screen.availableGeometry().height() - SCREEN_MARGIN)

    def fit_height(self) -> None:
        """Fix the window's height to its content, so only the width can be dragged.

        Above the cap the rows scroll. The height is applied only when it changes, so the resize
        it causes doesn't start another one, and a window manager that ignores it isn't fought.
        """
        content_height = self.content_height(self.width())
        cap = self.height_cap()
        # The scroll bar shows only while the height is capped. Left to show as needed, it would
        # stay after a narrower width briefly needed it: the content is narrower beside it, wraps
        # into more lines, and so keeps needing it.
        self._rows_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOn
            if content_height > cap
            else Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        height = min(content_height, cap)
        if height != self._fitted_height:
            self._fitted_height = height
            self.setFixedHeight(height)

    def _update_minimum_width(self) -> None:
        # The fixed columns and a small graph, and room for the scroll bar of a capped height.
        scroll_bar = self._rows_scroll.verticalScrollBar().sizeHint().width()
        self.setMinimumWidth(self._rows_widget.minimumSizeHint().width() + scroll_bar)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        # Problem lines wrap differently at another width.
        if event.size().width() != event.oldSize().width():
            self.fit_height()

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        handle = self.windowHandle()
        if handle is not None and not self._screen_connected:
            handle.screenChanged.connect(lambda _screen: self.fit_height())
            self._screen_connected = True
        self.fit_height()

    def graph_columns(self) -> int:
        """Return how many buckets a snapshot needs so the widest graph is full."""
        return max(1, min(HISTORY_BUCKETS, self._rows_scroll.viewport().width() // CELL_WIDTH))

    def show_snapshot(self, snapshot: EngineSnapshot) -> None:
        structure = _structure(snapshot)
        if structure != self._structure:
            self._rebuild(snapshot)
            self._structure = structure

        endpoint_rows = iter(self.endpoint_rows)
        for row in snapshot.rows:
            next(endpoint_rows).show_row(row, snapshot.bucket_seconds)
        for header, chain in zip(self.chain_headers, snapshot.chains, strict=True):
            header.show_chain(chain)
            for hop in chain.hops:
                next(endpoint_rows).show_row(hop, snapshot.bucket_seconds)

        self._apply_widths(snapshot)
        self._show_problems(problem_lines(snapshot))
        self.fit_height()

    def _rebuild(self, snapshot: EngineSnapshot) -> None:
        for widget in [*self.endpoint_rows, *self.chain_headers]:
            self._rows_layout.removeWidget(widget)
            widget.deleteLater()
        self.endpoint_rows = []
        self.chain_headers = []

        def add(widget: QWidget) -> None:
            # Insert above the problems and the stretch.
            self._rows_layout.insertWidget(self._rows_layout.indexOf(self.problems_section), widget)

        for _row in snapshot.rows:
            row_widget = EndpointRow()
            row_widget.expanded_changed.connect(self.fit_height)
            self.endpoint_rows.append(row_widget)
            add(row_widget)
        for chain in snapshot.chains:
            header = ChainHeader()
            self.chain_headers.append(header)
            add(header)
            for _hop in chain.hops:
                hop_widget = EndpointRow(indent=INDENT)
                hop_widget.expanded_changed.connect(self.fit_height)
                self.endpoint_rows.append(hop_widget)
                add(hop_widget)

    def _apply_widths(self, snapshot: EngineSnapshot) -> None:
        metrics = QFontMetrics(self.font())
        bold = self.font()
        bold.setBold(True)
        bold_metrics = QFontMetrics(bold)
        names = [
            *(bold_metrics.horizontalAdvance(row.endpoint.display_name) for row in snapshot.rows),
            *(bold_metrics.horizontalAdvance(chain.name) for chain in snapshot.chains),
            *(
                INDENT * 2 + bold_metrics.horizontalAdvance(hop.endpoint.display_name)
                for chain in snapshot.chains
                for hop in chain.hops
            ),
        ]
        routes = [
            metrics.horizontalAdvance(row.header.route.text())
            for row in self.endpoint_rows
            if row.header.route.text()
        ]
        widths = ColumnWidths(
            mark=metrics.horizontalAdvance(EXPANDED_MARK) + 2,
            name=min(MAX_NAME_WIDTH, max(names, default=0) + COLUMN_PADDING),
            latency=metrics.horizontalAdvance("9999.9ms") + COLUMN_PADDING,
            label=metrics.horizontalAdvance("(TCP:65535)") + COLUMN_PADDING,
            route=max(routes) + COLUMN_PADDING if routes else 0,
        )
        for row in self.endpoint_rows:
            row.apply_widths(widths)
        for header in self.chain_headers:
            header.apply_widths(widths)
        self._update_minimum_width()

    def _show_problems(self, problems: list[str]) -> None:
        """Show the problem lines below the rows, or hide the section while there are none."""
        self.problems.setText("\n".join(f"• {problem}" for problem in problems))
        self.problems_section.setVisible(bool(problems))

"""The painted latency graph: one column per bucket, newest on the right."""

from collections.abc import Sequence
from datetime import datetime

from PySide6.QtCore import QEvent, QRect, Qt
from PySide6.QtGui import QColor, QFont, QHelpEvent, QPainter, QPaintEvent
from PySide6.QtWidgets import QSizePolicy, QToolTip, QWidget

from hydraping.core.display import latency_text
from hydraping.core.graphcells import BAR_LEVELS, CellKind, GraphCell, classify_cells
from hydraping.core.snapshot import LayerView
from hydraping.core.store import Bucket
from hydraping.gui.colors import LATENCY_RGB, NO_DATA_RGB

# Pixels per bucket, including a one pixel gap after each column.
CELL_WIDTH = 8
CELL_GAP = 1
# Failure letters are drawn only in graphs at least this tall; a shorter column can't fit one.
MIN_LETTER_HEIGHT = 12
# A bold letter this size fits the width of one column.
LETTER_PIXEL_SIZE = 10
# A gap column is drawn in red at this opacity, so it reads as missing data, not as a failure.
GAP_ALPHA = 90

GRAPH_HEIGHT = 24
LAYER_GRAPH_HEIGHT = 16


def _qcolor(rgb: tuple[int, int, int], alpha: int = 255) -> QColor:
    return QColor(rgb[0], rgb[1], rgb[2], alpha)


def bucket_tooltip(bucket: Bucket | None, bucket_seconds: float) -> str:
    """Return the tooltip of one graph column: when, what the last result was, and the counts."""
    if bucket is None:
        return "No result"
    started = datetime.fromtimestamp(bucket.index * bucket_seconds).strftime("%H:%M:%S")
    result = bucket.last
    latency = latency_text(result)
    lines = [f"{started}  {latency.text} {latency.label}".rstrip()]
    if not result.success and result.error_message:
        lines.append(result.error_message)
    lines.append(f"{bucket.ok_count} OK, {bucket.failed_count} failed in this bucket")
    return "\n".join(lines)


class GraphWidget(QWidget):
    """Paint the cells that ``hydraping.core.graphcells`` decides, like the terminal graph.

    - A bar grows with the cell's level and has its latency colour.
    - A failure is a full-height red column with the letter of the layer that broke.
    - A short gap between results is a faint red column.
    - An empty bucket is a grey dot on the baseline.

    The widget shows as many of the newest cells as fit its width, right-aligned. Hovering over a
    column shows its bucket's time, last result and counts.
    """

    def __init__(self, height: int = GRAPH_HEIGHT, parent: QWidget | None = None):
        super().__init__(parent)
        self.setFixedHeight(height)
        self.setMinimumWidth(CELL_WIDTH * 10)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed)
        self._buckets: tuple[Bucket | None, ...] = ()
        self._cells: list[GraphCell] = []
        self._bucket_seconds = 1.0

    def set_layer(self, view: LayerView | None, bucket_seconds: float) -> None:
        """Show the buckets of ``view``, or an empty graph for None."""
        self._buckets = view.buckets if view is not None else ()
        self._cells = classify_cells(view.results()) if view is not None else []
        self._bucket_seconds = bucket_seconds
        self.update()

    def cells(self) -> list[GraphCell]:
        return list(self._cells)

    def visible_columns(self) -> int:
        """Return how many buckets fit the current width."""
        return max(0, self.width() // CELL_WIDTH)

    def _first_column_x(self, shown: int) -> int:
        return self.width() - shown * CELL_WIDTH

    def paintEvent(self, event: QPaintEvent) -> None:
        painter = QPainter(self)
        try:
            columns = self.visible_columns()
            cells = self._cells[-columns:] if columns else []
            # Columns with no bucket in the snapshot are drawn as empty ones.
            cells = [GraphCell(CellKind.EMPTY)] * (columns - len(cells)) + cells
            x = self._first_column_x(len(cells))
            font = QFont(self.font())
            font.setPixelSize(LETTER_PIXEL_SIZE)
            font.setBold(True)
            painter.setFont(font)
            for cell in cells:
                self._paint_cell(painter, QRect(x, 0, CELL_WIDTH - CELL_GAP, self.height()), cell)
                x += CELL_WIDTH
        finally:
            painter.end()

    def _paint_cell(self, painter: QPainter, rect: QRect, cell: GraphCell) -> None:
        if cell.kind == CellKind.EMPTY or cell.color is None:
            dot = QRect(rect.left() + rect.width() // 2 - 1, rect.bottom() - 1, 2, 2)
            painter.fillRect(dot, _qcolor(NO_DATA_RGB, 160))
            return
        color = LATENCY_RGB[cell.color]
        if cell.kind == CellKind.BAR:
            assert cell.level is not None
            height = max(2, round(rect.height() * (cell.level + 1) / BAR_LEVELS))
            bar = QRect(rect.left(), rect.bottom() - height + 1, rect.width(), height)
            painter.fillRect(bar, _qcolor(color))
            return
        alpha = GAP_ALPHA if cell.kind == CellKind.GAP else 255
        painter.fillRect(rect, _qcolor(color, alpha))
        if cell.letter and cell.kind == CellKind.FAILURE and rect.height() >= MIN_LETTER_HEIGHT:
            painter.setPen(QColor(Qt.GlobalColor.white))
            painter.drawText(rect, Qt.AlignmentFlag.AlignCenter, cell.letter)

    def bucket_at(self, x: int) -> tuple[bool, Bucket | None]:
        """Return whether ``x`` is over a column of the snapshot, and that column's bucket."""
        columns = self.visible_columns()
        buckets: Sequence[Bucket | None] = self._buckets[-columns:] if columns else ()
        offset = x - self._first_column_x(len(buckets))
        if offset < 0 or not buckets:
            return False, None
        index = offset // CELL_WIDTH
        if index >= len(buckets):
            return False, None
        return True, buckets[index]

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            assert isinstance(event, QHelpEvent)
            over_column, bucket = self.bucket_at(event.pos().x())
            if over_column:
                QToolTip.showText(
                    event.globalPos(), bucket_tooltip(bucket, self._bucket_seconds), self
                )
            else:
                QToolTip.hideText()
                event.ignore()
            return True
        return super().event(event)

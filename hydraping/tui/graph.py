"""Latency graph rendering for terminal UI."""

from rich.text import Text

from hydraping.core.graphcells import CellKind, GraphCell, classify_cells
from hydraping.core.models import CheckResult
from hydraping.tui.constants import RICH_COLORS


class LatencyGraph:
    """Renders latency history as a graph."""

    # Unicode block characters for graph bars (from empty to full). A bar of level N is
    # BLOCKS[N + 1].
    BLOCKS = ["·", "▁", "▂", "▃", "▄", "▅", "▆", "▇", "█"]
    EMPTY_CHAR = "·"  # Character for empty/padding areas

    def render(self, bucketed_results: list[CheckResult | None]) -> Text:
        """Render graph from pre-bucketed check results, one character per bucket.

        The graph stores no width. The caller sizes the list to the current terminal on every
        frame, so the graph follows resizes.

        ``hydraping.core.graphcells`` decides what each bucket shows; this method maps the cells
        to characters and Rich styles:
        - a bar of the cell's level, in its latency colour;
        - the failure letter (D, T, S, H, or "!" for a check without layers), in red;
        - "!" in red for a short gap between results;
        - a dim dot for empty buckets before the first result, after the last one, or in a long
          gap.

        Args:
            bucketed_results: List of CheckResult or None, where each position
                            represents a time bucket from oldest to newest.

        Returns:
            Text object with properly styled graph
        """
        graph_text = Text()
        for cell in classify_cells(bucketed_results):
            graph_text.append(*self._char_and_style(cell))
        return graph_text

    def _char_and_style(self, cell: GraphCell) -> tuple[str, str]:
        if cell.kind == CellKind.EMPTY or cell.color is None:
            return self.EMPTY_CHAR, "dim"
        style = RICH_COLORS[cell.color]
        if cell.kind == CellKind.BAR:
            assert cell.level is not None
            return self.BLOCKS[cell.level + 1], style
        assert cell.letter is not None
        return cell.letter, style

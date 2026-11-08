"""The top-bar icon, drawn into raw pixels without Qt.

StatusNotifierItem hosts take icons as ``IconPixmap``: a list of (width, height, bytes) with
ARGB32 pixels in network byte order, so each pixel is the bytes A, R, G, B, rows top to bottom.
"""

from collections.abc import Sequence

from hydraping.gui.tray_status import IconBar

# The sizes hosts ask for: 22 px panels, and 44 px on a display scaled to 200 %.
ICON_SIZES = (22, 44)
# Pixel sizes at 22 px. Larger icons scale them.
_MARGIN = 1
_GAP = 1
_MAX_BAR_WIDTH = 6
_MIN_BAR_HEIGHT = 2

TRANSPARENT = b"\x00\x00\x00\x00"


def render_icon(bars: Sequence[IconBar], size: int) -> bytes:
    """Return a ``size`` × ``size`` ARGB32 image with ``bars`` side by side, standing on the bottom.

    The bars share the width, at most ``_MAX_BAR_WIDTH`` each at 22 px, and are centred. Each bar
    is as tall as its ``height`` fraction of the space between the top and bottom margins.
    """
    if not bars:
        return TRANSPARENT * (size * size)
    scale = size / 22
    margin = max(1, round(_MARGIN * scale))
    gap = max(1, round(_GAP * scale))
    usable = size - 2 * margin
    bar_width = min(round(_MAX_BAR_WIDTH * scale), (usable - gap * (len(bars) - 1)) // len(bars))
    bar_width = max(1, bar_width)
    total_width = bar_width * len(bars) + gap * (len(bars) - 1)
    left = (size - total_width) // 2
    min_height = max(1, round(_MIN_BAR_HEIGHT * scale))

    pixels = bytearray(TRANSPARENT * (size * size))
    bottom = size - margin
    for index, bar in enumerate(bars):
        height = max(min_height, min(usable, round(bar.height * usable)))
        pixel = bytes((0xFF, *bar.rgb))
        x0 = left + index * (bar_width + gap)
        for y in range(bottom - height, bottom):
            start = (y * size + x0) * 4
            pixels[start : start + bar_width * 4] = pixel * bar_width
    return bytes(pixels)


def icon_pixmaps(bars: Sequence[IconBar]) -> list[tuple[int, int, bytes]]:
    """Return the icon in every size of ``ICON_SIZES``, as ``IconPixmap`` entries."""
    return [(size, size, render_icon(bars, size)) for size in ICON_SIZES]

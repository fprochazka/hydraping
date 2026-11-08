"""The GUI's colours as RGB, without Qt, so code that paints without Qt can use them too."""

from hydraping.core.graphcells import LatencyColor

RGB = tuple[int, int, int]

LATENCY_RGB: dict[LatencyColor, RGB] = {
    LatencyColor.GREEN: (0x3F, 0xB9, 0x50),
    LatencyColor.YELLOW: (0xD4, 0xB1, 0x06),
    LatencyColor.ORANGE: (0xF0, 0x88, 0x3E),
    LatencyColor.RED: (0xE5, 0x53, 0x4B),
}

# Empty graph columns, and rows without data.
NO_DATA_RGB: RGB = (0x8C, 0x8C, 0x8C)


def hex_color(rgb: RGB) -> str:
    """Return ``rgb`` as ``#rrggbb``, for Qt style sheets."""
    return "#{:02x}{:02x}{:02x}".format(*rgb)

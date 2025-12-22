"""UI constants and color thresholds for latency display."""

# Latency thresholds in milliseconds for color coding
LATENCY_GREEN_MAX = 50.0  # Below this: green (good)
LATENCY_YELLOW_MAX = 100.0  # Below this: yellow (medium)
LATENCY_ORANGE_MAX = 200.0  # Below this: orange (concerning)
# Above LATENCY_ORANGE_MAX: red (bad)

# Validate thresholds are strictly increasing to prevent division by zero
if not (0 < LATENCY_GREEN_MAX < LATENCY_YELLOW_MAX < LATENCY_ORANGE_MAX):
    raise ValueError(
        "Latency thresholds must be strictly increasing: "
        f"0 < {LATENCY_GREEN_MAX} < {LATENCY_YELLOW_MAX} < {LATENCY_ORANGE_MAX}"
    )


def get_latency_color(latency_ms: float) -> str:
    """Get color for latency value based on thresholds.

    Args:
        latency_ms: Latency in milliseconds

    Returns:
        Color name for Rich styling
    """
    if latency_ms < LATENCY_GREEN_MAX:
        return "green"
    elif latency_ms < LATENCY_YELLOW_MAX:
        return "yellow"
    elif latency_ms < LATENCY_ORANGE_MAX:
        return "orange1"
    else:
        return "red"

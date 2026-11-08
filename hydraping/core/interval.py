"""Adaptive probe interval: every second while the target is fast, less often while it is slow.

A probe that takes long is a sign that the link or the target is under strain, and probing it every
second adds to that strain. Each schedule therefore picks its next interval from the smoothed
duration of its own recent probes. The mapping is fixed: a smoothed duration of ``FAST_SECONDS`` or
less gives the minimum interval, ``SLOW_SECONDS`` or more gives the maximum, and the interval rises
linearly in between.
"""

# A probe at or under this duration is fast: the schedule stays at its minimum interval.
FAST_SECONDS = 0.1
# A probe at or over this duration is slow: the schedule goes to its maximum interval. A failed
# probe counts as this duration, whatever time it took.
SLOW_SECONDS = 0.5
# Weight of the newest probe in the moving average. At 0.5, one slow probe among fast ones moves the
# average halfway to slow, and three fast probes after a slow stretch bring it back under
# ``FAST_SECONDS``.
SMOOTHING = 0.5


def interval_for(average_seconds: float, minimum: float, maximum: float) -> float:
    """Return the interval for a smoothed probe duration, between ``minimum`` and ``maximum``.

    The result is ``minimum`` up to ``FAST_SECONDS``, ``maximum`` from ``SLOW_SECONDS`` on, and
    rises linearly in between. ``maximum`` may equal ``minimum``, which pins the interval.
    """
    if maximum < minimum:
        raise ValueError("maximum must be at least minimum")
    if average_seconds <= FAST_SECONDS:
        return minimum
    if average_seconds >= SLOW_SECONDS:
        return maximum
    position = (average_seconds - FAST_SECONDS) / (SLOW_SECONDS - FAST_SECONDS)
    return minimum + position * (maximum - minimum)


class ProbeDurations:
    """The exponential moving average of one schedule's probe durations, clamped to the slow mark.

    A new tracker reads as fast, so a schedule starts at its minimum interval. A success counts its
    wall time, capped at ``SLOW_SECONDS``: beyond that mark the interval is at its maximum anyway,
    and the cap keeps one very slow probe from holding the average up long after the target
    recovered. A failure counts as ``SLOW_SECONDS``.
    """

    def __init__(self) -> None:
        self.average = 0.0

    def record(self, duration_seconds: float, success: bool) -> float:
        """Fold one finished probe into the average and return the new average."""
        sample = min(duration_seconds, SLOW_SECONDS) if success else SLOW_SECONDS
        self.average += SMOOTHING * (sample - self.average)
        return self.average

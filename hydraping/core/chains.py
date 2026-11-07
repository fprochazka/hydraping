"""Chain diagnosis: which hop of a chain of dependent hops is the first one that is broken."""

from collections.abc import Sequence
from dataclasses import dataclass

from hydraping.core.models import CheckResult
from hydraping.core.store import Bucket


@dataclass(frozen=True)
class HopState:
    """What the diagnosis knows about one hop.

    ``result`` is the hop's primary layer result in the current bucket, or None when the hop has
    none. ``cause`` is the result of the layer that failed in the same probe, when the primary
    layer was skipped because of it; its error message is the one worth showing.
    """

    name: str
    result: CheckResult | None
    cause: CheckResult | None = None


@dataclass(frozen=True)
class ChainDiagnosis:
    # Index of the first broken hop, or None when no hop is broken.
    broken_at: int | None
    # The problem line, without the chain's name, or None when no hop is broken.
    message: str | None


def current_result(buckets: Sequence[Bucket | None]) -> CheckResult | None:
    """Return the result a chain diagnosis looks at, from a layer's buckets, oldest first.

    That is the open bucket's last result. While the open bucket has none yet, which is the case
    for up to one probe interval after it opens, the bucket that just closed stands in for it, so
    the diagnosis doesn't blink at every bucket boundary. Older buckets never count: a hop with no
    recent result is not known to be broken.
    """
    for bucket in reversed(buckets[-2:]):
        if bucket is not None:
            return bucket.last
    return None


def diagnose_chain(hops: Sequence[HopState]) -> ChainDiagnosis:
    """Return the first hop whose primary layer failed, and the problem line that reports it.

    The line names the hop, the layer that broke and its error, then the hops before it that
    answered, for example ``broken at Jumpbox (TCP: Connection refused); Router, Internet OK``.
    The walk stops at the first hop without a current result, such as one whose first probe is
    still running: the hops after it are not judged, because a failure there may only follow from
    the unanswered hop. With no broken hop before it, there is no diagnosis.
    """
    for index, hop in enumerate(hops):
        result = hop.result
        if result is None:
            break
        if result.success:
            continue
        layer = result.failed_layer or result.check_type
        error = hop.cause.error_message if hop.cause is not None else result.error_message
        message = f"broken at {hop.name} ({layer.value.upper()}: {error})"
        if index:
            message += f"; {', '.join(before.name for before in hops[:index])} OK"
        return ChainDiagnosis(broken_at=index, message=message)
    return ChainDiagnosis(broken_at=None, message=None)

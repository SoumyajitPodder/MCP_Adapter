"""Which events may be sampled away (brief §8.6)."""

from adapter_verify.observability.domain.attributes import Outcome
from adapter_verify.observability.domain.events import CallEvent


def must_keep(event: CallEvent) -> bool:
    """Errors, denials, state-changing calls, drift and idempotency changes are never sampled."""
    return (
        event.outcome is not Outcome.OK
        or (event.behavior is not None and event.behavior.changes_state)
        or event.drift_classification is not None
        or event.idempotency_state is not None
    )


def should_keep(event: CallEvent, *, ratio: float, draw: float) -> bool:
    """Keep ``event`` if it must be kept, else with probability ``ratio``.

    ``draw`` is a uniform number in [0, 1) supplied by the caller, keeping this pure.
    """
    if not 0.0 <= ratio <= 1.0:
        msg = "ratio must be within [0, 1]"
        raise ValueError(msg)
    if not 0.0 <= draw < 1.0:
        msg = "draw must be within [0, 1)"
        raise ValueError(msg)
    return must_keep(event) or draw < ratio

"""The one place the current request context lives (brief §8.1).

A module-level ``ContextVar`` is the approved exception to "no module-level state"
(DESIGN.md R-018): it is task-local, never shared between concurrent calls. Only
``ObservedEntry`` binds it; everything else reads.
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from adapter_kernel.context import RequestContext

_current: ContextVar[RequestContext | None] = ContextVar("adapter_request_context", default=None)


def current_request() -> RequestContext | None:
    return _current.get()


@contextmanager
def bound(ctx: RequestContext) -> Iterator[RequestContext]:
    """Make ``ctx`` current for the enclosed block, restoring the previous value on exit."""
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)

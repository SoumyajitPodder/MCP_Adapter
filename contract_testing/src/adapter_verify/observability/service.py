"""The outermost pipeline wrapper: correlation, the ``tool.call`` span, and its event (§8.1).

Transport-neutral on purpose (M1-Q1 is open): whatever serves MCP converts a request into an
``InboundCall`` and the result back.
"""

import re
from typing import Final, Protocol

from pydantic import Field, SecretStr

from adapter_kernel.context import RequestContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult
from adapter_verify.common.model import FrozenModel
from adapter_verify.common.ports import Clock, Entropy
from adapter_verify.idempotency.domain.keys import is_valid_key
from adapter_verify.observability.context import bound
from adapter_verify.observability.domain.attributes import Outcome, SpanAttributes, SpanName
from adapter_verify.observability.domain.events import CallEvent
from adapter_verify.observability.domain.exceptions import safe_exception_summary
from adapter_verify.observability.domain.ids import (
    UUID7_RANDOM_BYTES,
    is_valid_correlation_id,
    uuid7_from,
)
from adapter_verify.observability.domain.sampling import should_keep
from adapter_verify.observability.ports import Diagnostics, EventSink, SpanHandle, Telemetry

# MCP request ``params._meta`` keys (approved carrier, M0 §5).
# TODO(owner): replace the "adapter/" prefix with the organization's reverse-DNS namespace.
META_CORRELATION_ID: Final = "adapter/correlation-id"
META_IDEMPOTENCY_KEY: Final = "adapter/idempotency-key"
# Pending Romik (R-006): whether the version travels here or in the tool name.
META_TOOL_VERSION: Final = "adapter/tool-version"

# Caller-supplied names go into spans, events and the audit log, so malformed or oversized ones
# are rejected at the entry (like a bad correlation ID) and never reach a sink.
_TOOL_NAME: Final = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOOL_VERSION: Final = re.compile(r"[0-9A-Za-z.+-]{1,64}")
INVALID_TOOL: Final = "<invalid>"

_DENIALS: Final = frozenset(
    {ErrorCode.NOT_AUTHORIZED, ErrorCode.VERSION_NOT_PERMITTED, ErrorCode.SCOPE_VIOLATION}
)


class InboundCall(FrozenModel):
    """A tool call as received, before anything is trusted."""

    tool: str = Field(min_length=1)
    arguments: JsonObject
    meta: dict[str, str] = Field(default_factory=dict)
    credential: SecretStr | None = Field(
        default=None, description="Bearer token from the transport. Never logged."
    )


class Downstream(Protocol):
    """Everything after this stage: authentication, then the stage chain."""

    async def __call__(
        self, ctx: RequestContext, request: ToolRequest, credential: SecretStr | None
    ) -> ToolResult: ...


class ObservedEntry:
    def __init__(  # noqa: PLR0913 - one parameter per port
        self,
        *,
        telemetry: Telemetry,
        events: EventSink,
        diagnostics: Diagnostics,
        clock: Clock,
        entropy: Entropy,
        downstream: Downstream,
    ) -> None:
        self._telemetry = telemetry
        self._events = events
        self._diagnostics = diagnostics
        self._clock = clock
        self._entropy = entropy
        self._downstream = downstream

    async def handle(self, call: InboundCall) -> ToolResult:
        supplied = call.meta.get(META_CORRELATION_ID)
        bad_cid = supplied is not None and not is_valid_correlation_id(supplied)
        cid = supplied if supplied is not None and not bad_cid else self._new_id()
        version = call.meta.get(META_TOOL_VERSION)
        bad_tool = _TOOL_NAME.fullmatch(call.tool) is None
        bad_version = version is not None and _TOOL_VERSION.fullmatch(version) is None
        rejected = bad_cid or bad_tool or bad_version
        tool = INVALID_TOOL if bad_tool else call.tool
        started = self._clock.monotonic()

        with self._telemetry.span(
            SpanName.TOOL_CALL, SpanAttributes(correlation_id=cid, tool=tool)
        ) as span:
            ctx = RequestContext(
                correlation_id=cid,
                correlation_id_generated=cid != supplied,
                trace_id=span.trace_id,
                tool=tool,
                requested_version=None if bad_version else version,
                idempotency_key=call.meta.get(META_IDEMPOTENCY_KEY),
            )
            with bound(ctx):
                if rejected:
                    result = _failure(ErrorCode.INVALID_INPUT, cid)
                else:
                    result = await self._run(
                        ctx, ToolRequest(arguments=call.arguments), call.credential
                    )
            result = _with_correlation_id(result, cid)
            self._record(span, ctx, result, (self._clock.monotonic() - started) * 1000)
        return result

    async def _run(
        self, ctx: RequestContext, request: ToolRequest, credential: SecretStr | None
    ) -> ToolResult:
        try:
            return await self._downstream(ctx, request, credential)
        except Exception as exc:  # noqa: BLE001 - the one boundary that turns anything into INTERNAL
            self._diagnostics.internal_error(ctx.correlation_id, safe_exception_summary(exc))
            return _failure(ErrorCode.INTERNAL, ctx.correlation_id)

    def _record(
        self, span: SpanHandle, ctx: RequestContext, result: ToolResult, latency_ms: float
    ) -> None:
        outcome = result.outcome
        error_code = outcome.error.code if isinstance(outcome, ToolFailure) else None
        status = _outcome(error_code)
        if error_code is not None:
            span.fail(error_code)
        span.update(SpanAttributes(outcome=status, latency_ms=latency_ms))
        key = ctx.idempotency_key
        self._events.emit(
            CallEvent(
                timestamp=self._clock.now(),
                trace_id=span.trace_id,
                span_id=span.span_id,
                correlation_id=ctx.correlation_id,
                agent_id=None,
                tool=ctx.tool,
                semantic_version=None,
                stage=SpanName.TOOL_CALL,
                latency_ms=latency_ms,
                outcome=status,
                error_code=error_code,
                # An invalid key is rejected by §7; it never reaches a log sink.
                idempotency_key=key if key is not None and is_valid_key(key) else None,
            )
        )

    def _new_id(self) -> str:
        unix_ms = int(self._clock.now().timestamp() * 1000)
        return str(uuid7_from(unix_ms, self._entropy.token_bytes(UUID7_RANDOM_BYTES)))


class SampledEventSink:
    """Applies the §8.6 sampling rules in front of another sink."""

    def __init__(self, inner: EventSink, *, read_ratio: float, entropy: Entropy) -> None:
        self._inner = inner
        self._ratio = read_ratio
        self._entropy = entropy

    def emit(self, event: CallEvent) -> None:
        draw = int.from_bytes(self._entropy.token_bytes(7), "big") / (1 << 56)
        if should_keep(event, ratio=self._ratio, draw=draw):
            self._inner.emit(event)


def _outcome(error_code: ErrorCode | None) -> Outcome:
    if error_code is None:
        return Outcome.OK
    return Outcome.DENIED if error_code in _DENIALS else Outcome.ERROR


def _failure(code: ErrorCode, correlation_id: str) -> ToolResult:
    return ToolResult(
        outcome=ToolFailure(
            error=AdapterError(code=code), meta=ResponseMeta(correlation_id=correlation_id)
        ),
        delivery=None,
    )


def _with_correlation_id(result: ToolResult, correlation_id: str) -> ToolResult:
    """Echo the ID the call ran under, whatever downstream put in ``_meta`` (§3.4)."""
    meta = result.outcome.meta.model_copy(update={"correlation_id": correlation_id})
    return result.model_copy(update={"outcome": result.outcome.model_copy(update={"meta": meta})})

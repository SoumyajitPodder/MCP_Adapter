import pytest
from pydantic import ValidationError

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import AdapterError, ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    Next,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_kernel.tooldef import Behavior

pytestmark = pytest.mark.unit


def _request_ctx() -> RequestContext:
    return RequestContext(
        correlation_id="synthetic-cid-1",
        correlation_id_generated=False,
        trace_id="synthetic-trace-1",
        tool="order.get",
        requested_version=None,
        idempotency_key=None,
    )


def _call_ctx() -> CallContext:
    return CallContext(
        request=_request_ctx(),
        agent_id="synthetic-agent",
        semantic_version="1.2.0",
        behavior=Behavior.READ_ONLY,
    )


def test_call_context_exposes_request_fields() -> None:
    ctx = _call_ctx()
    assert ctx.correlation_id == "synthetic-cid-1"
    assert ctx.tool == "order.get"


def test_contexts_are_frozen() -> None:
    ctx = _call_ctx()
    with pytest.raises(ValidationError):
        ctx.agent_id = "someone-else"  # type: ignore[misc]


def test_contexts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        RequestContext.model_validate({**_request_ctx().model_dump(), "extra": "x"})


def test_call_context_requires_agent_id() -> None:
    with pytest.raises(ValidationError):
        CallContext(
            request=_request_ctx(),
            agent_id="",
            semantic_version="1.2.0",
            behavior=Behavior.READ_ONLY,
        )


def test_strict_mode_rejects_coercion() -> None:
    with pytest.raises(ValidationError):
        ResponseMeta.model_validate({"correlation_id": "c", "replayed": "true"})


@pytest.mark.parametrize(
    ("status", "possible"),
    [
        (DeliveryStatus.NOT_SENT, False),
        (DeliveryStatus.REJECTED_NO_EFFECT, False),
        (DeliveryStatus.SENT_NO_RESPONSE, True),
        (DeliveryStatus.ACKED, True),
    ],
)
def test_upstream_effect_possible(status: DeliveryStatus, *, possible: bool) -> None:
    assert status.upstream_effect_possible is possible


def test_tool_result_round_trips_through_json_by_discriminator() -> None:
    meta = ResponseMeta(correlation_id="synthetic-cid-1")
    failure = ToolResult(
        outcome=ToolFailure(error=AdapterError(code=ErrorCode.INTERNAL), meta=meta),
        delivery=DeliveryStatus.NOT_SENT,
    )
    success = ToolResult(
        outcome=ToolSuccess(content={"status": "IN_PROGRESS"}, meta=meta),
        delivery=DeliveryStatus.ACKED,
    )
    for result in (failure, success):
        assert ToolResult.model_validate_json(result.model_dump_json()) == result


def test_tool_result_requires_an_explicit_delivery_status() -> None:
    outcome = ToolSuccess(content={}, meta=ResponseMeta(correlation_id="synthetic-cid-1"))
    with pytest.raises(ValidationError):
        ToolResult(outcome=outcome)  # type: ignore[call-arg]
    assert ToolResult(outcome=outcome, delivery=None).delivery is None


def test_with_outcome_keeps_the_delivery_status() -> None:
    meta = ResponseMeta(correlation_id="synthetic-cid-1")
    acked = ToolResult(outcome=ToolSuccess(content={}, meta=meta), delivery=DeliveryStatus.ACKED)
    failed = acked.with_outcome(
        ToolFailure(error=AdapterError(code=ErrorCode.CONTRACT_VIOLATION), meta=meta)
    )
    assert isinstance(failed.outcome, ToolFailure)
    assert failed.delivery is DeliveryStatus.ACKED


@pytest.mark.asyncio
async def test_a_stage_can_wrap_next() -> None:
    meta = ResponseMeta(correlation_id="synthetic-cid-1")

    async def terminal(ctx: CallContext, request: ToolRequest) -> ToolResult:  # noqa: ARG001
        return ToolResult(
            outcome=ToolSuccess(content=request.arguments, meta=meta), delivery=DeliveryStatus.ACKED
        )

    class Echo:
        name = "echo"

        async def __call__(
            self, ctx: CallContext, request: ToolRequest, call_next: Next
        ) -> ToolResult:
            return await call_next(ctx, request)

    result = await Echo()(_call_ctx(), ToolRequest(arguments={"order_id": "88213"}), terminal)
    assert isinstance(result.outcome, ToolSuccess)
    assert result.outcome.content == {"order_id": "88213"}

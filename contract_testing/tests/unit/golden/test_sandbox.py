from datetime import UTC, datetime
from pathlib import Path

import pytest

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.credentials import CredentialMap
from adapter_verify.access.fakes import SentinelSecretManager
from adapter_verify.access.service import CredentialScoper
from adapter_verify.common.fakes import ManualClock
from adapter_verify.golden.domain.tasks import FixtureRef
from adapter_verify.golden.sandbox import DefinitionInputStage, FixtureBackend
from tests.unit.golden.support import Repo

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _ctx(tool: str = "order.get", behavior: Behavior = Behavior.READ_ONLY) -> CallContext:
    request = RequestContext(
        correlation_id="c",
        correlation_id_generated=False,
        trace_id="t",
        tool=tool,
        requested_version=None,
        idempotency_key=None,
    )
    return CallContext(request=request, agent_id="a", semantic_version="1.2.0", behavior=behavior)


def _ref(when: JsonObject | None) -> FixtureRef:
    return FixtureRef(tool="order.get", payload="fixtures/golden/x.synthetic.json", when=when)


async def test_fixture_backend_selects_by_arguments_and_counts() -> None:
    backend = FixtureBackend(
        [(_ref({"order_id": "2"}), {"n": 2}), (_ref(None), {"n": 0})], credentials=None
    )
    second = await backend(_ctx(), ToolRequest(arguments={"order_id": "2"}))
    other = await backend(_ctx(), ToolRequest(arguments={"order_id": "9"}))
    assert isinstance(second.outcome, ToolSuccess)
    assert second.outcome.content == {"n": 2}
    assert second.delivery is DeliveryStatus.ACKED
    assert isinstance(other.outcome, ToolSuccess)
    assert other.outcome.content == {"n": 0}
    missing = await backend(_ctx("order.cancel", Behavior.MUTATING), ToolRequest(arguments={}))
    assert isinstance(missing.outcome, ToolFailure)
    assert missing.delivery is DeliveryStatus.NOT_SENT
    state = backend.state()
    assert state.calls_by_tool == {"order.get": 2, "order.cancel": 1}
    assert state.writes_performed == 0  # nothing acknowledged a write


async def test_fixture_backend_without_a_credential_never_sends() -> None:
    scoper = CredentialScoper(
        CredentialMap(bindings=()),
        SentinelSecretManager(),
        ManualClock(datetime(2026, 9, 26, tzinfo=UTC)),
        ttl_s=60,
    )
    backend = FixtureBackend([(_ref(None), {"n": 0})], credentials=scoper)
    result = await backend(_ctx(), ToolRequest(arguments={}))
    assert isinstance(result.outcome, ToolFailure)
    assert result.outcome.error.code is ErrorCode.UPSTREAM_UNAVAILABLE


async def test_input_stage_fails_closed_without_a_definition(tmp_path: Path) -> None:
    definitions = Repo.create(tmp_path).workspace().definitions
    stage = DefinitionInputStage(definitions)

    async def ok(ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        return ToolResult(
            outcome=ToolSuccess(content={}, meta=ResponseMeta(correlation_id=ctx.correlation_id)),
            delivery=DeliveryStatus.ACKED,
        )

    unknown = _ctx("order.list")
    result = await stage(unknown, ToolRequest(arguments={}), ok)
    assert isinstance(result.outcome, ToolFailure)
    assert result.outcome.error.code is ErrorCode.INTERNAL
    passed = await stage(_ctx(), ToolRequest(arguments={"order_id": "1"}), ok)
    assert isinstance(passed.outcome, ToolSuccess)

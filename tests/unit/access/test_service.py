from datetime import UTC, datetime

import pytest
from pydantic import SecretStr

from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_kernel.tooldef import Behavior
from adapter_verify.access.domain.credentials import CredentialBinding, CredentialMap
from adapter_verify.access.domain.lint import PolicyDocument
from adapter_verify.access.fakes import SentinelSecretManager, StaticTokenVerifier
from adapter_verify.access.ports import SecretUnavailableError
from adapter_verify.access.service import (
    AccessObserver,
    AccessStage,
    Authenticator,
    CredentialScoper,
    PolicyLoadError,
    PolicyRegistry,
)
from adapter_verify.common.fakes import ManualClock
from adapter_verify.observability.audit_service import AuditTrail
from adapter_verify.observability.domain.attributes import Outcome, SpanName
from adapter_verify.observability.fakes import (
    MemoryAuditStore,
    MemoryDiagnostics,
    MemoryEventSink,
    RecordingTelemetry,
)
from tests.unit.access.support import CATALOG, READER, WRITER, document, policy

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

_AT = datetime(2026, 9, 25, tzinfo=UTC)


class World:
    def __init__(self, documents: list[PolicyDocument] | None = None) -> None:
        self.documents = documents or [document(READER), document(WRITER)]
        self.clock = ManualClock(_AT)
        self.telemetry = RecordingTelemetry()
        self.events = MemoryEventSink()
        self.store = MemoryAuditStore()
        self.diagnostics = MemoryDiagnostics()
        self.registry = PolicyRegistry(lambda: self.documents, CATALOG, self.diagnostics)
        observer = AccessObserver(
            self.telemetry,
            self.events,
            AuditTrail(self.store, self.clock),
            self.diagnostics,
            self.clock,
        )
        self.verifier = StaticTokenVerifier({"t-reader": "reader", "t-writer": "writer"})
        self.authenticator = Authenticator(self.verifier, CATALOG, self.registry, observer)
        self.stage = AccessStage(self.registry, observer)
        self.next_calls = 0

    async def terminal(self, ctx: CallContext, request: ToolRequest) -> ToolResult:
        del request
        self.next_calls += 1
        return ToolResult(
            outcome=ToolSuccess(content={}, meta=ResponseMeta(correlation_id=ctx.correlation_id)),
            delivery=DeliveryStatus.ACKED,
        )

    def audit_rules(self) -> list[object]:
        return [r.event.detail["rule_id"] for rs in self.store.records.values() for r in rs]


def _request(tool: str = "order.get", version: str | None = None) -> RequestContext:
    return RequestContext(
        correlation_id="c-1",
        correlation_id_generated=False,
        trace_id="t",
        tool=tool,
        requested_version=version,
        idempotency_key=None,
    )


def _ctx(agent: str, tool: str, version: str, behavior: Behavior) -> CallContext:
    return CallContext(
        request=_request(tool, version), agent_id=agent, semantic_version=version, behavior=behavior
    )


def _code(result: ToolResult) -> ErrorCode:
    assert isinstance(result.outcome, ToolFailure)
    return result.outcome.error.code


async def test_registry_refuses_to_start_on_lint_findings() -> None:
    bad = [document(policy(grants=(("billing.get", ">=1.0.0,<2.0.0"),)))]
    with pytest.raises(PolicyLoadError) as caught:
        PolicyRegistry(lambda: bad, CATALOG, MemoryDiagnostics())
    assert {f.rule_id for f in caught.value.findings} == {"POLICY_UNKNOWN_TOOL"}


async def test_reload_keeps_last_good_set_and_alerts() -> None:
    w = World()
    good = w.registry.current
    w.documents = [document(READER, "wrong-name.yaml")]
    assert not w.registry.reload()
    assert w.registry.current is good
    assert w.diagnostics.internal_errors == [
        ("access", "policy reload rejected: POLICY_FILE_NAME_MISMATCH")
    ]
    w.documents = [document(READER)]
    assert w.registry.reload()
    assert w.registry.current.digest != good.digest


@pytest.mark.parametrize(
    ("credential", "tool", "rule"),
    [
        (None, "order.get", "ACCESS_TOKEN_MISSING"),
        (SecretStr("forged"), "order.get", "ACCESS_TOKEN_INVALID"),
        (SecretStr("t-reader"), "billing.get", "ACCESS_UNKNOWN_TOOL"),
    ],
)
async def test_authentication_denials_are_audited(
    credential: SecretStr | None, tool: str, rule: str
) -> None:
    w = World()
    result = await w.authenticator.authenticate(_request(tool), credential)
    assert isinstance(result, ToolResult)
    assert _code(result) is ErrorCode.NOT_AUTHORIZED
    assert w.audit_rules() == [rule]
    (event,) = w.events.events
    assert (event.stage, event.outcome) == (SpanName.ACCESS_AUTHENTICATE, Outcome.DENIED)


async def test_authentication_resolves_default_version_and_behavior() -> None:
    w = World()
    ctx = await w.authenticator.authenticate(_request(), SecretStr("t-reader"))
    assert isinstance(ctx, CallContext)
    assert (ctx.agent_id, ctx.semantic_version, ctx.behavior) == (
        "reader",
        "1.2.0",
        Behavior.READ_ONLY,
    )
    assert w.audit_rules() == []


async def test_read_allow_is_evented_not_audited() -> None:
    w = World()
    result = await w.stage(
        _ctx("reader", "order.get", "1.2.0", Behavior.READ_ONLY),
        ToolRequest(arguments={}),
        w.terminal,
    )
    assert isinstance(result.outcome, ToolSuccess)
    assert w.next_calls == 1
    assert w.audit_rules() == []
    assert w.events.events[0].outcome is Outcome.OK
    assert w.telemetry.spans[0].attributes["adapter.rule_id"] == "ACCESS_GRANTED:reader/order.get/0"


async def test_denial_is_audited_and_stops_the_chain() -> None:
    w = World()
    result = await w.stage(
        _ctx("reader", "order.get", "2.0.0", Behavior.READ_ONLY),
        ToolRequest(arguments={}),
        w.terminal,
    )
    assert _code(result) is ErrorCode.VERSION_NOT_PERMITTED
    assert w.next_calls == 0
    assert w.audit_rules() == ["ACCESS_VERSION_OUT_OF_RANGE"]
    (chain,) = w.store.records
    assert chain == "agent:reader"


async def test_mutating_allow_is_audited() -> None:
    w = World()
    result = await w.stage(
        _ctx("writer", "order.cancel", "1.0.0", Behavior.MUTATING),
        ToolRequest(arguments={}),
        w.terminal,
    )
    assert isinstance(result.outcome, ToolSuccess)
    assert w.audit_rules() == ["ACCESS_GRANTED:writer/order.cancel/1"]


async def test_audit_outage_fails_closed_for_mutating_allows() -> None:
    w = World()
    w.store.unavailable = True
    result = await w.stage(
        _ctx("writer", "order.cancel", "1.0.0", Behavior.MUTATING),
        ToolRequest(arguments={}),
        w.terminal,
    )
    assert _code(result) is ErrorCode.INTERNAL
    assert w.next_calls == 0
    assert w.diagnostics.internal_errors == [("c-1", "access audit unavailable")]


async def test_audit_outage_keeps_denials_denied() -> None:
    w = World()
    w.store.unavailable = True
    result = await w.stage(
        _ctx("reader", "order.cancel", "1.0.0", Behavior.MUTATING),
        ToolRequest(arguments={}),
        w.terminal,
    )
    assert _code(result) is ErrorCode.NOT_AUTHORIZED
    assert w.next_calls == 0


async def test_list_tools_matches_what_calls_allow() -> None:
    w = World()
    assert await w.authenticator.list_tools(None) is None
    assert await w.authenticator.list_tools(SecretStr("forged")) is None
    shown = await w.authenticator.list_tools(SecretStr("t-reader"))
    assert shown is not None
    assert [(e.tool, e.version) for e in shown] == [("order.get", "1.2.0")]


async def test_credentials_are_scoped_per_tool_and_cached() -> None:
    clock = ManualClock(_AT)
    secrets = SentinelSecretManager()
    names = CredentialMap(
        bindings=(
            CredentialBinding(tool="order.get", versions=">=1.0.0,<2.0.0", secret_name="om/read"),
        )
    )
    scoper = CredentialScoper(names, secrets, clock, ttl_s=60)
    ctx = _ctx("reader", "order.get", "1.2.0", Behavior.READ_ONLY)
    first = await scoper.credential_for(ctx)
    await scoper.credential_for(ctx)
    assert secrets.requests == ["om/read"]
    clock.advance(61)
    assert (await scoper.credential_for(ctx)).get_secret_value() == first.get_secret_value()
    assert secrets.requests == ["om/read", "om/read"]
    with pytest.raises(SecretUnavailableError):
        await scoper.credential_for(_ctx("writer", "order.cancel", "1.0.0", Behavior.MUTATING))

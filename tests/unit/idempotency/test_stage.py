import asyncio

import pytest

from adapter_kernel.context import CallContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonValue
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult, ToolSuccess
from adapter_kernel.tooldef import Behavior
from adapter_verify.idempotency.domain.records import AlertKind, IdemState, RecordKey
from adapter_verify.idempotency.fakes import Fault
from adapter_verify.observability.domain.attributes import SpanName
from adapter_verify.observability.fakes import MemoryPayloadStore
from adapter_verify.observability.ports import PayloadKind
from tests.unit.idempotency.support import ARGS, TOOL, World, context, error

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]

KEY = RecordKey(agent_id="writer", tool=TOOL, key="k-1")


def _state(world: World) -> IdemState | None:
    record = world.store.records.get(KEY)
    return None if record is None else record.state


def _alerts(world: World) -> list[AlertKind]:
    return [kind for kind, _, _ in world.alerts.alerts]


async def test_read_only_tools_pass_through_without_a_key() -> None:
    world = World()
    result = await world.call(context(None, behavior=Behavior.READ_ONLY))
    assert isinstance(result.outcome, ToolSuccess)
    assert world.connector.calls == 1
    assert world.store.records == {}
    assert world.telemetry.spans == []


@pytest.mark.parametrize(
    ("key", "arguments", "code"),
    [
        (None, ARGS, ErrorCode.IDEMPOTENCY_KEY_REQUIRED),
        ("bad key!", ARGS, ErrorCode.INVALID_INPUT),
        ("k" * 129, ARGS, ErrorCode.INVALID_INPUT),
        ("k-1", {"amount": float("nan")}, ErrorCode.INVALID_INPUT),
    ],
)
async def test_rejected_before_the_store(
    key: str | None, arguments: dict[str, JsonValue], code: ErrorCode
) -> None:
    world = World()
    result = await world.call(context(key, behavior=Behavior.DESTRUCTIVE), arguments)
    assert error(result) is code
    assert world.connector.calls == 0
    assert world.store.records == {}
    assert world.events.events[-1].error_code is code
    assert world.events.events[-1].idempotency_key == (key if key == "k-1" else None)


async def test_completed_call_replays_without_a_second_upstream_call() -> None:
    world = World()
    first = await world.call()
    assert isinstance(first.outcome, ToolSuccess)
    assert first.outcome.content == {"effect": 1}
    record = world.store.records[KEY]
    assert record.state is IdemState.COMPLETED
    assert record.result_ref is not None

    world.clock.advance(5)
    replay = await world.call(context(cid="c-2"))
    assert isinstance(replay.outcome, ToolSuccess)
    assert replay.outcome.content == {"effect": 1}
    assert replay.outcome.meta.replayed is True
    assert replay.outcome.meta.correlation_id == "c-2"
    assert replay.delivery is None
    assert world.connector.calls == 1
    assert world.audited() == [(None, "RESERVED"), ("RESERVED", "COMPLETED")]
    assert world.events.events[-1].replayed is True
    names = [s.name for s in world.telemetry.spans]
    assert names == [
        SpanName.IDEMPOTENCY_RESERVE,
        SpanName.IDEMPOTENCY_SETTLE,
        SpanName.IDEMPOTENCY_RESERVE,
    ]


async def test_same_key_with_different_arguments_or_version_conflicts() -> None:
    world = World()
    await world.call()
    changed = await world.call(arguments={**ARGS, "reason": "other"})
    assert error(changed) is ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    other_version = await world.call(context(version="1.1.0"))
    assert error(other_version) is ErrorCode.IDEMPOTENCY_KEY_CONFLICT
    assert world.connector.calls == 1


async def test_keys_are_scoped_per_agent_and_tool() -> None:
    world = World()
    await world.call()
    await world.call(context(agent="other-writer"))
    await world.call(context(tool="order.refund"))
    assert world.connector.calls == 3
    assert len(world.store.records) == 3


@pytest.mark.parametrize("fault", [Fault.NOT_SENT, Fault.REJECTED_NO_EFFECT])
async def test_provably_unsent_failure_frees_the_key_for_a_retry(fault: Fault) -> None:
    world = World()
    world.connector.script.append(fault)
    failed = await world.call()
    assert error(failed) in {ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.INVALID_INPUT}
    assert _state(world) is IdemState.FAILED_RETRYABLE
    attempt = world.store.records[KEY].attempt_id

    retried = await world.call()
    assert isinstance(retried.outcome, ToolSuccess)
    assert world.connector.calls == 2
    assert _state(world) is IdemState.COMPLETED
    assert world.store.records[KEY].attempt_id != attempt  # fencing token rotated
    assert ("FAILED_RETRYABLE", "RESERVED") in world.audited()
    assert world.alerts.alerts == []


async def test_sent_without_response_becomes_unknown_and_is_never_re_executed() -> None:
    world = World()
    world.connector.mode = Fault.SENT_NO_RESPONSE
    first = await world.call()
    assert error(first) is ErrorCode.RECONCILIATION_PENDING
    assert _state(world) is IdemState.UNKNOWN
    assert _alerts(world) == [AlertKind.OUTCOME_UNKNOWN]

    world.connector.mode = Fault.ACK
    again = await world.call()
    assert error(again) is ErrorCode.RECONCILIATION_PENDING
    assert world.connector.calls == 1


async def test_acknowledged_then_failed_completes_with_the_error() -> None:
    world = World()
    world.connector.mode = Fault.ACK_THEN_FAIL
    first = await world.call()
    assert error(first) is ErrorCode.CONTRACT_VIOLATION
    record = world.store.records[KEY]
    assert (record.state, record.error_code) == (IdemState.COMPLETED, ErrorCode.CONTRACT_VIOLATION)
    assert _alerts(world) == [AlertKind.EFFECT_WITH_ERROR]

    replay = await world.call()
    assert isinstance(replay.outcome, ToolFailure)
    assert replay.outcome.error.code is ErrorCode.CONTRACT_VIOLATION
    assert replay.outcome.meta.replayed is True
    assert world.connector.calls == 1


async def test_exception_after_send_records_unknown_and_propagates() -> None:
    world = World()
    world.connector.mode = Fault.RAISE_AFTER_SEND
    with pytest.raises(ConnectionResetError):
        await world.call()
    assert _state(world) is IdemState.UNKNOWN
    assert _alerts(world) == [AlertKind.OUTCOME_UNKNOWN]
    assert world.audited()[-1] == ("RESERVED", "UNKNOWN")


async def test_success_without_delivery_status_completes_and_alerts() -> None:
    world = World()
    world.connector.mode = Fault.SUCCESS_WITHOUT_STATUS
    result = await world.call()
    assert isinstance(result.outcome, ToolSuccess)
    assert _state(world) is IdemState.COMPLETED
    assert _alerts(world) == [AlertKind.SUCCESS_WITHOUT_ACK]


async def test_concurrent_identical_calls_make_one_upstream_call() -> None:
    world = World()
    world.connector.gate = asyncio.Event()
    first = asyncio.create_task(world.call())
    await world.connector.entered.wait()
    world.clock.advance(10)
    others = await asyncio.gather(*(world.call() for _ in range(49)))
    world.connector.gate.set()
    done = await first

    assert isinstance(done.outcome, ToolSuccess)
    assert world.connector.calls == 1
    for result in others:
        assert isinstance(result.outcome, ToolFailure)
        assert result.outcome.error.code is ErrorCode.DUPLICATE_IN_PROGRESS
        assert result.outcome.error.retry_after_ms == 20_000


async def test_expired_lease_answers_reconciliation_pending_until_resolved() -> None:
    world = World()
    world.connector.gate = asyncio.Event()
    first = asyncio.create_task(world.call())
    await world.connector.entered.wait()
    world.clock.advance(31)
    assert error(await world.call()) is ErrorCode.RECONCILIATION_PENDING
    world.connector.gate.set()
    assert isinstance((await first).outcome, ToolSuccess)  # not swept yet: still its row


async def test_lease_swept_mid_call_fences_out_a_non_acked_settlement() -> None:
    world = World()

    async def slow_then_unsent(ctx: CallContext, request: ToolRequest) -> ToolResult:
        world.clock.advance(31)
        await world.maintenance.sweep()
        world.connector.mode = Fault.NOT_SENT
        return await world.connector(ctx, request)

    result = await world.stage(context(), ToolRequest(arguments=ARGS), slow_then_unsent)
    assert error(result) is ErrorCode.RECONCILIATION_PENDING
    assert _state(world) is IdemState.UNKNOWN  # the sweeper's verdict stands
    assert ("c-1", "idempotency settlement fenced out") in world.diagnostics.internal_errors
    assert _alerts(world) == [AlertKind.LEASE_EXPIRED]


async def test_late_acked_completion_by_the_same_attempt_moves_unknown_to_completed() -> None:
    world = World()

    async def slow_ack(ctx: CallContext, request: ToolRequest) -> ToolResult:
        world.clock.advance(31)
        await world.maintenance.sweep()
        return await world.connector(ctx, request)

    result = await world.stage(context(), ToolRequest(arguments=ARGS), slow_ack)
    assert isinstance(result.outcome, ToolSuccess)
    assert _state(world) is IdemState.COMPLETED
    # Separate chains: the sweeper (system) and the agent.
    assert {("RESERVED", "UNKNOWN"), ("UNKNOWN", "COMPLETED")} <= set(world.audited())


async def test_reservation_audit_failure_releases_the_key_and_fails_closed() -> None:
    world = World()
    world.audit_store.unavailable = True
    result = await world.call()
    assert error(result) is ErrorCode.INTERNAL
    assert world.connector.calls == 0
    assert _state(world) is IdemState.FAILED_RETRYABLE

    world.audit_store.unavailable = False
    assert isinstance((await world.call()).outcome, ToolSuccess)


async def test_release_failure_leaves_the_lease_to_expire() -> None:
    world = World()
    world.audit_store.unavailable = True
    world.store.fail_transitions = True
    assert error(await world.call()) is ErrorCode.INTERNAL
    assert _state(world) is IdemState.RESERVED
    assert ("c-1", "idempotency release failed") in world.diagnostics.internal_errors


async def test_store_outage_fails_closed_before_anything_is_sent() -> None:
    world = World()
    world.store.unavailable = True
    assert error(await world.call()) is ErrorCode.INTERNAL
    assert world.connector.calls == 0
    assert ("c-1", "idempotency store unavailable") in world.diagnostics.internal_errors


async def test_settlement_write_failure_returns_the_result_and_alerts() -> None:
    world = World()

    async def ack_then_break_store(ctx: CallContext, request: ToolRequest) -> ToolResult:
        world.store.fail_transitions = True
        return await world.connector(ctx, request)

    result = await world.stage(context(), ToolRequest(arguments=ARGS), ack_then_break_store)
    assert isinstance(result.outcome, ToolSuccess)
    assert _state(world) is IdemState.RESERVED  # the lease will expire into UNKNOWN
    assert _alerts(world) == [AlertKind.SETTLEMENT_FAILED]


async def test_settlement_audit_failure_keeps_the_state_and_logs() -> None:
    world = World()

    async def ack_then_break_audit(ctx: CallContext, request: ToolRequest) -> ToolResult:
        world.audit_store.unavailable = True
        return await world.connector(ctx, request)

    result = await world.stage(context(), ToolRequest(arguments=ARGS), ack_then_break_audit)
    assert isinstance(result.outcome, ToolSuccess)
    assert _state(world) is IdemState.COMPLETED
    assert ("c-1", "idempotency audit unavailable") in world.diagnostics.internal_errors


class _BrokenPayloads(MemoryPayloadStore):
    async def put(self, correlation_id: str, kind: PayloadKind, body: JsonValue) -> str:
        del correlation_id, kind, body
        msg = "payload store down"
        raise OSError(msg)


async def test_result_that_cannot_be_stored_escalates_as_unknown() -> None:
    world = World()
    world.stage._payloads = _BrokenPayloads()
    result = await world.call()
    assert error(result) is ErrorCode.RECONCILIATION_PENDING
    assert _state(world) is IdemState.UNKNOWN
    assert _alerts(world) == [AlertKind.OUTCOME_UNKNOWN]


async def test_replay_with_missing_payload_fails_without_re_executing() -> None:
    world = World()
    await world.call()
    world.payloads._bodies.clear()
    assert error(await world.call()) is ErrorCode.INTERNAL
    assert world.connector.calls == 1
    assert _alerts(world) == [AlertKind.REPLAY_UNAVAILABLE]


async def test_replay_of_a_non_object_payload_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    world = World()
    await world.call()

    async def listy(ref: str) -> JsonValue:
        del ref
        return [1, 2]

    monkeypatch.setattr(world.payloads, "get", listy)
    assert error(await world.call()) is ErrorCode.INTERNAL
    assert _alerts(world) == [AlertKind.REPLAY_UNAVAILABLE]


@pytest.mark.parametrize("purged", [False, True])
async def test_lost_rereserve_race_answers_in_progress(
    monkeypatch: pytest.MonkeyPatch, purged: bool
) -> None:
    world = World()
    world.connector.script.append(Fault.NOT_SENT)
    await world.call()

    async def lose(reservation: object) -> bool:
        del reservation
        return False

    async def gone(key: RecordKey) -> None:
        del key

    monkeypatch.setattr(world.store, "rereserve", lose)
    if purged:
        monkeypatch.setattr(world.store, "get", gone)
    busy = await world.call()
    assert isinstance(busy.outcome, ToolFailure)
    assert busy.outcome.error.code is ErrorCode.DUPLICATE_IN_PROGRESS
    assert busy.outcome.error.retry_after_ms == 100
    assert world.connector.calls == 1

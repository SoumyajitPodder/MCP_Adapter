"""Hypothesis state machine over the §7.4/§7.5 tables: random calls, faults, time, sweeps and
manual resolutions against the real stage with in-memory ports.

Invariants: an effect is attempted at most once per key unless a person declared it had none;
every replay returns the first result; no call leaves a row RESERVED.
"""

import asyncio
from collections import defaultdict

import pytest
from hypothesis import settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, invariant, rule

from adapter_kernel.context import CallContext
from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject, JsonValue
from adapter_kernel.pipeline import ToolFailure, ToolRequest, ToolResult, ToolSuccess
from adapter_verify.idempotency.domain.records import IdemState, RecordKey
from adapter_verify.idempotency.fakes import Fault
from adapter_verify.idempotency.service import Operator, Resolution
from tests.unit.idempotency.support import TOOL, World, context

pytestmark = pytest.mark.property

KEYS = ("k-1", "k-2")
ARGUMENTS: tuple[JsonObject, ...] = ({"order_id": "o-1"}, {"order_id": "o-2"})
# Faults after which the backend may have changed state.
_EFFECT_POSSIBLE = frozenset(
    {
        Fault.ACK,
        Fault.ACK_THEN_FAIL,
        Fault.SENT_NO_RESPONSE,
        Fault.RAISE_AFTER_SEND,
        Fault.SUCCESS_WITHOUT_STATUS,
    }
)


class IdempotencyMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.world = World()
        self.effects: dict[str, int] = defaultdict(int)
        self.first_success: dict[str, JsonValue] = {}
        self.next_fault = Fault.ACK

    async def _terminal(self, ctx: CallContext, request: ToolRequest) -> ToolResult:
        key = ctx.request.idempotency_key or ""
        if self.next_fault in _EFFECT_POSSIBLE:
            self.effects[key] += 1
        self.world.connector.mode = self.next_fault
        return await self.world.connector(ctx, request)

    @rule(key=st.sampled_from(KEYS), args=st.sampled_from(ARGUMENTS), fault=st.sampled_from(Fault))
    def call(self, key: str, args: JsonObject, fault: Fault) -> None:
        self.next_fault = fault
        try:
            result = asyncio.run(
                self.world.stage(context(key), ToolRequest(arguments=args), self._terminal)
            )
        except ConnectionResetError:
            return
        outcome = result.outcome
        if isinstance(outcome, ToolSuccess) and outcome.content:
            if outcome.meta.replayed:
                assert outcome.content == self.first_success[key]
            else:
                self.first_success[key] = outcome.content
        if (
            isinstance(outcome, ToolFailure)
            and outcome.error.code is ErrorCode.DUPLICATE_IN_PROGRESS
        ):
            raise AssertionError("no call is in flight between steps")

    @rule(seconds=st.integers(0, 40))
    def wait(self, seconds: int) -> None:
        self.world.clock.advance(seconds)

    @rule()
    def sweep(self) -> None:
        asyncio.run(self.world.maintenance.sweep())

    @rule(key=st.sampled_from(KEYS), resolution=st.sampled_from(Resolution))
    def resolve(self, key: str, resolution: Resolution) -> None:
        record = asyncio.run(
            self.world.maintenance.resolve(
                RecordKey(agent_id="writer", tool=TOOL, key=key),
                resolution,
                Operator(name="ops", os_user="tester"),
                "checked the backend",
            )
        )
        if record is not None and resolution is Resolution.FAILED:
            self.effects[key] = 0  # a person declared the attempt had no effect

    @invariant()
    def at_most_one_effect_per_key(self) -> None:
        assert all(count <= 1 for count in self.effects.values()), dict(self.effects)

    @invariant()
    def nothing_left_reserved(self) -> None:
        states = {r.state for r in self.world.store.records.values()}
        assert IdemState.RESERVED not in states


IdempotencyMachine.TestCase.settings = settings(
    IdempotencyMachine.TestCase.settings, stateful_step_count=30
)
test_idempotency_state_machine = IdempotencyMachine.TestCase

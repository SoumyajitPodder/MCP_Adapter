"""The deterministic assertion layers (brief §6.4): calls, sequence, state.

Each check is a pure function that returns the first failure it finds, or None. They run in
order and short-circuit, so the answer judge never runs on a task that already failed a hard
check.
"""

from collections.abc import Sequence
from enum import StrEnum

import rfc8785
from pydantic import Field, JsonValue

from adapter_kernel.errors import ErrorCode
from adapter_kernel.jsontypes import JsonObject
from adapter_verify.common.model import FrozenModel
from adapter_verify.golden.domain.tasks import ArgsMatch, ExpectedCall, GoldenTask


class Layer(StrEnum):
    CALLS = "calls"
    SEQUENCE = "sequence"
    STATE = "state"
    ANSWER = "answer"


class CallRecord(FrozenModel):
    """One tool call as the sandbox saw it: the authoritative record, not the agent's claim."""

    tool: str = Field(description="Tool name as called.")
    version: str | None = Field(description="Resolved version; None when the tool is unknown.")
    arguments: JsonObject = Field(description="Arguments as sent (synthetic data only).")
    error_code: ErrorCode | None = Field(description="Agent-facing error, if the call failed.")


class SandboxState(FrozenModel):
    """The stub backend after a run."""

    writes_performed: int = Field(ge=0, description="State-changing calls acknowledged.")
    calls_by_tool: dict[str, int] = Field(description="Calls that reached the backend.")


class LayerFailure(FrozenModel):
    layer: Layer
    expected: str = Field(description="What the task required.")
    actual: str = Field(description="What happened.")


def json_equal(a: JsonValue, b: JsonValue) -> bool:
    """JSON equality (RFC 8785): key order and 1 vs 1.0 don't matter; true is not 1."""
    try:
        return bool(rfc8785.dumps(a) == rfc8785.dumps(b))
    except rfc8785.CanonicalizationError:
        return False


def args_match(expected: ExpectedCall, arguments: JsonObject) -> bool:
    if expected.args_match is ArgsMatch.EXACT:
        return json_equal(expected.args, arguments)
    return all(k in arguments and json_equal(v, arguments[k]) for k, v in expected.args.items())


def matches(expected: ExpectedCall, call: CallRecord) -> bool:
    return (
        call.tool == expected.tool
        and (expected.semantic_version is None or call.version == expected.semantic_version)
        and args_match(expected, call.arguments)
    )


def describe(expected: ExpectedCall) -> str:
    version = f"@{expected.semantic_version}" if expected.semantic_version else ""
    args = rfc8785.dumps(expected.args).decode()
    return f"{expected.tool}{version} {expected.args_match.value} {args}"


def describe_calls(calls: Sequence[CallRecord]) -> str:
    if not calls:
        return "no calls"
    return "; ".join(
        f"{c.tool}@{c.version or '?'} {_safe_dumps(c.arguments)}"
        + (f" -> {c.error_code.value}" if c.error_code else "")
        for c in calls
    )


def _safe_dumps(value: JsonObject) -> str:
    try:
        return rfc8785.dumps(value).decode()
    except rfc8785.CanonicalizationError:
        return "<not canonical JSON>"


def check_calls(task: GoldenTask, calls: Sequence[CallRecord]) -> LayerFailure | None:
    """Every expected call happened, each matched by a distinct actual call (order aside)."""
    unused = list(calls)
    for expected in task.expect_calls:
        found = next((c for c in unused if matches(expected, c)), None)
        if found is None:
            return LayerFailure(
                layer=Layer.CALLS, expected=describe(expected), actual=describe_calls(calls)
            )
        unused.remove(found)
    return None


def check_sequence(task: GoldenTask, calls: Sequence[CallRecord]) -> LayerFailure | None:
    """Nothing forbidden was attempted (a denied attempt still counts), order and exactness."""
    forbidden = [c for c in calls if c.tool in set(task.expect_no_calls)]
    if forbidden:
        return LayerFailure(
            layer=Layer.SEQUENCE,
            expected=f"no calls to {', '.join(sorted(task.expect_no_calls))}",
            actual=describe_calls(forbidden),
        )
    if task.ordered and not _in_order(task.expect_calls, calls):
        return LayerFailure(
            layer=Layer.SEQUENCE,
            expected="in order: " + "; ".join(describe(e) for e in task.expect_calls),
            actual=describe_calls(calls),
        )
    if task.exact_calls and len(calls) != len(task.expect_calls):
        return LayerFailure(
            layer=Layer.SEQUENCE,
            expected=f"exactly {len(task.expect_calls)} call(s)",
            actual=describe_calls(calls),
        )
    return None


def _in_order(expected: Sequence[ExpectedCall], calls: Sequence[CallRecord]) -> bool:
    position = 0
    for e in expected:
        while position < len(calls) and not matches(e, calls[position]):
            position += 1
        if position == len(calls):
            return False
        position += 1
    return True


def check_state(task: GoldenTask, state: SandboxState) -> LayerFailure | None:
    want = task.expect_state
    if want is None:
        return None
    if want.writes_performed is not None and state.writes_performed != want.writes_performed:
        return LayerFailure(
            layer=Layer.STATE,
            expected=f"writes_performed={want.writes_performed}",
            actual=f"writes_performed={state.writes_performed}",
        )
    for tool, count in sorted(want.calls_by_tool.items()):
        actual = state.calls_by_tool.get(tool, 0)
        if actual != count:
            return LayerFailure(
                layer=Layer.STATE,
                expected=f"{tool} reached the backend {count} time(s)",
                actual=f"{actual} time(s)",
            )
    return None

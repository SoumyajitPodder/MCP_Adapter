"""Thresholds, quarantine recommendations (§6.6) and the promotion gate (§6.9, R-013)."""

from collections.abc import Mapping, Sequence
from enum import IntEnum

from pydantic import Field

from adapter_verify.common.model import FrozenModel
from adapter_verify.golden.domain.results import (
    ErrorKind,
    RunOutcome,
    RunRecord,
    SuiteResults,
    TaskResult,
    TaskVerdict,
)
from adapter_verify.golden.domain.tasks import GoldenTask, QuarantineList

_UNDECIDED = frozenset({RunOutcome.NOT_JUDGED, RunOutcome.JUDGE_UNCALIBRATED})


def task_verdict(threshold: int, runs: Sequence[RunRecord]) -> TaskVerdict:
    passed = sum(r.outcome is RunOutcome.PASS for r in runs)
    if passed >= threshold:
        return TaskVerdict.PASS
    undecided = sum(r.outcome in _UNDECIDED or r.error is ErrorKind.MODEL_UNAVAILABLE for r in runs)
    return TaskVerdict.INCOMPLETE if passed + undecided >= threshold else TaskVerdict.FAIL


def quarantine_recommended(current: TaskResult, previous: TaskResult | None) -> bool:
    """Failed twice in a row with nothing it depends on changed (§6.6). Never applied here."""
    return (
        previous is not None
        and current.verdict is TaskVerdict.FAIL
        and previous.verdict is TaskVerdict.FAIL
        and current.input_digest == previous.input_digest
    )


def touches(task: GoldenTask, tool: str, version: str, default_version: str | None) -> bool:
    """Whether a promotion of tool@version affects the task."""
    if tool not in task.tools():
        return False
    pinned = {c.semantic_version for c in task.expect_calls if c.tool == tool}
    pinned.discard(None)
    return version in pinned if pinned else version == default_version or default_version is None


class GateExit(IntEnum):
    PASS = 0
    FAIL = 1
    QUARANTINE_BLOCK = 2


class GateDecision(FrozenModel):
    exit: GateExit
    lines: tuple[str, ...] = Field(description="One line per task considered, plus a summary.")


def gate(  # noqa: PLR0913 - each input is a separate source of truth
    tool: str,
    version: str,
    *,
    tasks: Sequence[GoldenTask],
    digests: Mapping[str, str],
    results: SuiteResults,
    quarantine: QuarantineList,
    default_version: str | None,
) -> GateDecision:
    """Promotion of tool@version passes only on current, passing evidence for every task.

    Stale evidence (inputs changed since the run), missing tasks, incomplete verdicts and zero
    coverage all fail closed. A quarantined task blocks unless a waiver names the tool.
    """
    relevant = sorted(
        (t for t in tasks if touches(t, tool, version, default_version)), key=lambda t: t.task_id
    )
    if not relevant:
        return GateDecision(
            exit=GateExit.FAIL, lines=(f"NO_COVERAGE\tno golden task touches {tool}@{version}",)
        )
    lines: list[str] = []
    failed = blocked = False
    for task in relevant:
        entry = quarantine.get(task.task_id)
        if entry is not None:
            waived = entry.waiver is not None and tool in entry.waiver.tools
            lines.append(f"{'WAIVED' if waived else 'QUARANTINED'}\t{task.task_id}")
            blocked |= not waived
            continue
        result = results.task(task.task_id)
        if result is None:
            lines.append(f"MISSING\t{task.task_id}\tnot in the results")
            failed = True
        elif result.input_digest != digests[task.task_id]:
            lines.append(f"STALE\t{task.task_id}\tinputs changed since the run")
            failed = True
        elif result.verdict is not TaskVerdict.PASS:
            lines.append(f"{result.verdict.value.upper()}\t{task.task_id}")
            failed = True
        else:
            lines.append(f"PASS\t{task.task_id}")
    exit_code = GateExit.FAIL if failed else GateExit.QUARANTINE_BLOCK if blocked else GateExit.PASS
    return GateDecision(exit=exit_code, lines=tuple(lines))

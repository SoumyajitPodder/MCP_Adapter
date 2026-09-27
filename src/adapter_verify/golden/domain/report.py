"""The suite report (brief §6.10): expected vs actual per failure, and what changed.

Plain Markdown for stdout, ``--report`` and ``$GITHUB_STEP_SUMMARY``.
"""

from collections.abc import Mapping

from adapter_verify.golden.domain.results import (
    RunOutcome,
    SuiteResults,
    TaskResult,
    TaskVerdict,
)


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render(results: SuiteResults, description_diffs: Mapping[str, str]) -> str:
    """``description_diffs``: tool key → unified diff of its description since the base."""
    counts = {v: sum(t.verdict is v for t in results.tasks) for v in TaskVerdict}
    lines = [
        "## Golden tasks",
        "",
        f"{len(results.tasks)} task(s): {counts[TaskVerdict.PASS]} pass, "
        f"{counts[TaskVerdict.FAIL]} fail, {counts[TaskVerdict.INCOMPLETE]} incomplete. "
        f"Run `{results.run_id}` at `{results.git_sha or 'unknown'}`.",
        "",
    ]
    if results.judge_model is None and counts[TaskVerdict.INCOMPLETE]:
        lines += [
            "No answer judge is configured, so tasks with `expect_answer` can't pass yet: "
            "their deterministic layers ran, the answer layer did not.",
            "",
        ]
    elif results.judge_calibrated is False:
        lines += [
            "**The judge failed calibration; its verdicts are not trusted.** "
            "`adapter-verify golden calibrate` shows why.",
            "",
        ]
    if results.budget_exceeded:
        lines += [f"**Suite token budget ({results.token_budget}) exceeded; runs stopped.**", ""]
    lines += [
        "| Task | Agent | Verdict | Passing runs | Why it ran |",
        "| --- | --- | --- | --- | --- |",
    ]
    for t in results.tasks:
        passed = sum(r.outcome is RunOutcome.PASS for r in t.runs)
        why = "; ".join(t.selected_because) or "selected"
        verdict = t.verdict.value.upper() + (" (quarantined)" if t.quarantined else "")
        lines.append(
            f"| `{t.task_id}` | `{t.agent}` | {verdict} | {passed}/{len(t.runs)} "
            f"(need {t.threshold}) | {_cell(why)} |"
        )
    failures = [t for t in results.tasks if t.verdict is not TaskVerdict.PASS]
    if failures:
        lines += ["", "### Details", ""]
        for t in failures:
            lines += _details(t)
    recommended = [t.task_id for t in results.tasks if t.quarantine_recommended]
    if recommended:
        lines += [
            "",
            "### Quarantine recommended",
            "",
            "Failed twice in a row with unchanged inputs. Quarantine needs a reviewed PR to "
            "`golden_tasks/quarantine.yaml`; it is never applied automatically.",
            "",
            *(f"- `{task_id}`" for task_id in recommended),
        ]
    if description_diffs:
        lines += ["", "### Tool descriptions changed", ""]
        for key, diff in sorted(description_diffs.items()):
            lines += [f"`{key}`:", "", "```diff", diff.rstrip("\n"), "```", ""]
    return "\n".join(lines).rstrip("\n") + "\n"


def _details(task: TaskResult) -> list[str]:
    lines = [f"**`{task.task_id}`**: {task.verdict.value}", ""]
    for run in task.runs:
        if run.outcome is RunOutcome.FAIL and run.failure is not None:
            f = run.failure
            lines += [
                f"- run {run.index}: **{f.layer.value}** layer failed",
                f"  - expected: {_cell(f.expected)}",
                f"  - actual: {_cell(f.actual)}",
            ]
        elif run.outcome is RunOutcome.ERROR and run.error is not None:
            detail = f": {_cell(run.detail)}" if run.detail else ""
            lines.append(f"- run {run.index}: error **{run.error.value}**{detail}")
        elif run.outcome is not RunOutcome.PASS:
            lines.append(f"- run {run.index}: {run.outcome.value}")
    return [*lines, ""]

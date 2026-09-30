"""Impact report (brief §5.8): what changed, which rule, which tools, and which agents can call
them — the "who needs to retest" answer, joined from the access policies."""

from collections.abc import Mapping, Sequence

from adapter_verify.contract_ci.domain.checks import GateResult, Status


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def render_markdown(result: GateResult, callers: Mapping[str, Sequence[str]]) -> str:
    """``callers``: tool → agent IDs granted any version of it."""
    lines = [
        f"## Contract check: {result.status.value.upper()} (exit {result.exit_code})",
        "",
    ]
    if result.review_override:
        lines += ["Review required, passed by the `contract-review-approved` label.", ""]
    lines += ["| Check | Subject | Status | Note |", "| --- | --- | --- | --- |"]
    lines += [
        f"| {r.check.value} | `{r.subject}` | {r.status.value} | {_cell(r.note or '')} |"
        for r in result.results
    ]
    # A passing result can still carry findings: a MAJOR bump passes with its breaking changes,
    # and those are exactly what the agents' owners need to retest against.
    detailed = [r for r in result.results if r.status is not Status.PASS or r.findings]
    for r in detailed:
        lines += ["", f"### {r.check.value}: `{r.subject}`", ""]
        lines += ["| Rule | Class | Operation | Path | Detail |", "| --- | --- | --- | --- | --- |"]
        lines += [
            f"| `{f.rule_id}` | {f.classification.value} | {_cell(f.operation or '-')} "
            f"| `{_cell(f.path or '-')}` | {_cell(f.detail + (f' ({f.note})' if f.note else ''))} |"
            for f in r.findings
        ]
        agents = ", ".join(
            f"`{a}`" for a in sorted({a for t in r.tools for a in callers.get(t, ())})
        )
        tools = ", ".join(f"`{t}`" for t in r.tools) or "-"
        lines += ["", f"Affected tools: {tools}. Agents to retest: {agents or 'none'}."]
    return "\n".join(lines) + "\n"

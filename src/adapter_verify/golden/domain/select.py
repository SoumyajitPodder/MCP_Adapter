"""Which tasks a change affects (brief §6.7), and the input digest that makes results stale."""

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from pathlib import PurePosixPath

from pydantic import Field

from adapter_kernel.shape import canonical_json
from adapter_verify.common.model import FrozenModel
from adapter_verify.golden.domain.definitions import ToolDefinition
from adapter_verify.golden.domain.tasks import AgentConfig, GoldenTask


class Layout(FrozenModel):
    """Repo-relative locations the selection rules read (POSIX paths)."""

    tasks_dir: str = "golden_tasks"
    definitions_dir: str = "catalog/definitions"
    catalog_file: str = "catalog/tools.yaml"
    policies_dir: str = "policies"
    quarantine_file: str = "golden_tasks/quarantine.yaml"
    full_suite: tuple[str, ...] = Field(
        default=("src/", "packages/", "contracts/", "mappings/", "pyproject.toml", "uv.lock"),
        description="Prefixes whose change re-runs everything (adapter code or version change).",
    )


def input_digest(
    task: GoldenTask,
    definitions: Iterable[ToolDefinition],
    fixture_digests: Mapping[str, str],
    agent: AgentConfig | None,
    policy: object | None,
) -> str:
    """SHA-256 over everything a task's outcome depends on; other digests mean stale results."""
    touched = sorted(
        (d for d in definitions if d.tool in task.tools()), key=lambda d: (d.tool, d.version)
    )
    body = {
        "task": task.model_dump(mode="json"),
        "definitions": {d.key: [d.description_digest(), d.schema_digest()] for d in touched},
        "fixtures": {f.payload: fixture_digests.get(f.payload) for f in task.fixtures},
        "agent": None if agent is None else agent.model_dump(mode="json"),
        "policy": policy,
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def select(
    tasks: Sequence[GoldenTask], changed_paths: Iterable[str], layout: Layout
) -> dict[str, list[str]]:
    """task_id → reasons it must run. Unrelated changes select nothing."""
    selected: dict[str, list[str]] = {}

    def add(task: GoldenTask, reason: str) -> None:
        reasons = selected.setdefault(task.task_id, [])
        if reason not in reasons:
            reasons.append(reason)

    everything: list[str] = []
    for raw in sorted(set(changed_paths)):
        path = PurePosixPath(raw)
        text = path.as_posix()
        if text == layout.quarantine_file:
            continue
        if text == layout.catalog_file or any(
            text == p or text.startswith(p) for p in layout.full_suite
        ):
            everything.append(text)
            continue
        for task in tasks:
            reason = _reason(task, path, layout)
            if reason is not None:
                add(task, reason)
    if everything:
        shown = ", ".join(everything[:3]) + (", ..." if len(everything) > 3 else "")  # noqa: PLR2004
        for task in tasks:
            add(task, f"full suite: {len(everything)} adapter file(s) changed ({shown})")
    return selected


def _reason(task: GoldenTask, path: PurePosixPath, layout: Layout) -> str | None:
    text = path.as_posix()
    if path.is_relative_to(layout.definitions_dir):
        tool = path.relative_to(layout.definitions_dir).parts[0]
        return f"definition of {tool} changed" if tool in task.tools() else None
    reasons = {
        **{f.payload: f"fixture {PurePosixPath(f.payload).name} changed" for f in task.fixtures},
        f"{layout.tasks_dir}/{task.agent}/agent.yaml": f"agent config of {task.agent} changed",
        f"{layout.tasks_dir}/{task.agent}/{task.task_id}.yaml": "task file changed",
        f"{layout.policies_dir}/{task.agent}.yaml": f"access policy of {task.agent} changed",
    }
    return reasons.get(text)

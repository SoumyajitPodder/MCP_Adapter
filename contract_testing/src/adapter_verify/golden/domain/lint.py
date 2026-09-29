"""Golden lint (brief §6.1: malformed task files fail CI). Pure; the loader supplies documents."""

from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Final

from pydantic import Field, JsonValue

from adapter_verify.access.domain.evaluate import evaluate
from adapter_verify.access.domain.policy import PolicySet, ToolCatalog
from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.contracts import CanonicalContract
from adapter_verify.golden.domain.definitions import ToolDefinition, argument_error, conforms
from adapter_verify.golden.domain.tasks import (
    SYNTHETIC_FIXTURE,
    AgentConfig,
    ArgsMatch,
    GoldenTask,
    QuarantineList,
)

type _Add = Callable[[str, str, str], None]

RULES: Final[dict[str, str]] = {
    "GOLDEN_SCHEMA": "a task, agent config, definition or quarantine file does not validate",
    "GOLDEN_FILE_NAME_MISMATCH": "files must be <agent>/<task_id>.yaml and <agent>/agent.yaml",
    "GOLDEN_DUPLICATE_ID": "two tasks share a task_id",
    "GOLDEN_UNKNOWN_TOOL": "a fixture or expected call names a tool that is not in the catalog",
    "GOLDEN_UNKNOWN_VERSION": "an expected call pins a version that is not in the catalog",
    "GOLDEN_UNKNOWN_AGENT": "the task's agent has no access policy",
    "GOLDEN_AGENT_CONFIG_MISSING": "the task's agent has no agent.yaml",
    "GOLDEN_TOOL_NOT_GRANTED": "an expected call is not granted to the agent (can never pass)",
    "GOLDEN_EXPECT_CONFLICT": "a tool is both expected and forbidden",
    "GOLDEN_ARGS_INVALID": "expected arguments violate the tool's input definition",
    "GOLDEN_FIXTURE_MISSING": "a fixture file is absent or not a JSON object",
    "GOLDEN_FIXTURE_NOT_SYNTHETIC": "fixtures must be fixtures/golden/<name>.synthetic.json",
    "GOLDEN_FIXTURE_CONTRACT": "a fixture does not match the tool's canonical contract",
    "GOLDEN_THRESHOLD": "threshold exceeds runs, or a writing task doesn't require every run",
    "GOLDEN_DEFINITION_MISSING": "a catalog tool version has no definition",
    "GOLDEN_DEFINITION_ORPHAN": "a definition has no catalog entry, or its file name is wrong",
    "GOLDEN_QUARANTINE_UNKNOWN_TASK": "quarantine.yaml names a task that doesn't exist",
}


class Document[M](FrozenModel):
    """A file as loaded: the parsed model, or why it could not be parsed."""

    path: str = Field(description="Repo-relative POSIX path.")
    model: M | None
    load_error: str | None


class GoldenFinding(FrozenModel):
    rule_id: str
    path: str
    detail: str


class Inputs(FrozenModel):
    """Everything golden lint reads."""

    tasks: tuple[Document[GoldenTask], ...]
    agents: tuple[Document[AgentConfig], ...]
    definitions: tuple[Document[ToolDefinition], ...]
    quarantine: Document[QuarantineList]
    fixtures: dict[str, JsonValue] = Field(
        description="Fixture path → parsed JSON; absent if unreadable."
    )
    tasks_dir: str
    definitions_dir: str


@dataclass(frozen=True)
class _Refs:
    """What task references are checked against."""

    catalog: ToolCatalog
    policies: PolicySet
    definitions: Mapping[str, ToolDefinition]
    contracts: Mapping[str, CanonicalContract]
    fixtures: Mapping[str, JsonValue]


def lint(
    inputs: Inputs,
    catalog: ToolCatalog,
    policies: PolicySet,
    contracts: Sequence[CanonicalContract],
) -> list[GoldenFinding]:
    findings: list[GoldenFinding] = []

    def add(rule: str, path: str, detail: str) -> None:
        findings.append(GoldenFinding(rule_id=rule, path=path, detail=detail))

    load_errors = [
        *((d.path, d.load_error) for d in inputs.tasks),
        *((d.path, d.load_error) for d in inputs.agents),
        *((d.path, d.load_error) for d in inputs.definitions),
        (inputs.quarantine.path, inputs.quarantine.load_error),
    ]
    for path, error in load_errors:
        if error is not None:
            add("GOLDEN_SCHEMA", path, error)

    _lint_definitions(inputs, catalog, add)
    agents = _lint_agents(inputs, add)
    refs = _Refs(
        catalog=catalog,
        policies=policies,
        definitions={d.model.key: d.model for d in inputs.definitions if d.model is not None},
        contracts={c.key: c for c in contracts},
        fixtures=inputs.fixtures,
    )
    tasks = [(d.path, d.model) for d in inputs.tasks if d.model is not None]
    counts = Counter(task.task_id for _, task in tasks)
    for path, task in tasks:
        if counts[task.task_id] > 1:
            add("GOLDEN_DUPLICATE_ID", path, task.task_id)
        if path != f"{inputs.tasks_dir}/{task.agent}/{task.task_id}.yaml":
            add("GOLDEN_FILE_NAME_MISMATCH", path, f"expected {task.agent}/{task.task_id}.yaml")
        if policies.get(task.agent) is None:
            add("GOLDEN_UNKNOWN_AGENT", path, task.agent)
        if task.agent not in agents:
            add("GOLDEN_AGENT_CONFIG_MISSING", path, task.agent)
        writes = _lint_calls(path, task, refs, add)
        _lint_fixtures(path, task, refs, add)
        state = task.expect_state
        writes |= bool(state and state.writes_performed)
        if task.threshold > task.runs or (writes and task.threshold != task.runs):
            add("GOLDEN_THRESHOLD", path, f"threshold {task.threshold} of {task.runs}")

    quarantine = inputs.quarantine.model
    if quarantine is not None:
        for entry in quarantine.entries:
            if entry.task_id not in counts:
                add("GOLDEN_QUARANTINE_UNKNOWN_TASK", inputs.quarantine.path, entry.task_id)
    return sorted(findings, key=lambda f: (f.path, f.rule_id, f.detail))


def _lint_definitions(inputs: Inputs, catalog: ToolCatalog, add: _Add) -> None:
    catalog_keys = {f"{e.tool}@{e.version}" for e in catalog.tools}
    defined: set[str] = set()
    for doc in inputs.definitions:
        d = doc.model
        if d is None:
            continue
        defined.add(d.key)
        expected = f"{inputs.definitions_dir}/{d.tool}/{d.version}.yaml"
        if doc.path != expected or d.key not in catalog_keys:
            add("GOLDEN_DEFINITION_ORPHAN", doc.path, d.key)
    for key in sorted(catalog_keys - defined):
        add("GOLDEN_DEFINITION_MISSING", inputs.definitions_dir, key)


def _lint_agents(inputs: Inputs, add: _Add) -> set[str]:
    agents: set[str] = set()
    for doc in inputs.agents:
        if doc.model is None:
            continue
        if doc.path != f"{inputs.tasks_dir}/{doc.model.agent_id}/agent.yaml":
            add("GOLDEN_FILE_NAME_MISMATCH", doc.path, f"agent_id is {doc.model.agent_id}")
        agents.add(doc.model.agent_id)
    return agents


def _lint_calls(path: str, task: GoldenTask, refs: _Refs, add: _Add) -> bool:
    """Check expected calls; return whether any of them changes state."""
    for tool in sorted({c.tool for c in task.expect_calls} & set(task.expect_no_calls)):
        add("GOLDEN_EXPECT_CONFLICT", path, tool)
    writes = False
    for call in task.expect_calls:
        if call.tool not in refs.catalog.names():
            add("GOLDEN_UNKNOWN_TOOL", path, call.tool)
            continue
        entry = refs.catalog.resolve(call.tool, call.semantic_version)
        if entry is None:
            add("GOLDEN_UNKNOWN_VERSION", path, f"{call.tool}@{call.semantic_version}")
            continue
        writes |= entry.behavior.changes_state
        decision = evaluate(refs.policies, task.agent, entry.tool, entry.semver, entry.behavior)
        if not decision.allowed:
            add("GOLDEN_TOOL_NOT_GRANTED", path, f"{task.agent} -> {entry.tool}@{entry.version}")
        definition = refs.definitions.get(f"{entry.tool}@{entry.version}")
        bad = None if definition is None else _args_error(definition, call.args, call.args_match)
        if bad is not None:
            add("GOLDEN_ARGS_INVALID", path, f"{entry.tool}: {bad}")
    return writes


def _lint_fixtures(path: str, task: GoldenTask, refs: _Refs, add: _Add) -> None:
    for fixture in task.fixtures:
        if fixture.tool not in refs.catalog.names():
            add("GOLDEN_UNKNOWN_TOOL", path, fixture.tool)
        if SYNTHETIC_FIXTURE.fullmatch(fixture.payload) is None:
            add("GOLDEN_FIXTURE_NOT_SYNTHETIC", path, fixture.payload)
        body = refs.fixtures.get(fixture.payload)
        if not isinstance(body, dict):
            add("GOLDEN_FIXTURE_MISSING", path, fixture.payload)
            continue
        entry = refs.catalog.resolve(fixture.tool, None)
        contract = None if entry is None else refs.contracts.get(f"{entry.tool}@{entry.version}")
        bad = None if contract is None else contract_error(contract, body)
        if bad is not None:
            add("GOLDEN_FIXTURE_CONTRACT", path, f"{fixture.payload}: {bad}")


def _args_error(
    definition: ToolDefinition, args: dict[str, JsonValue], mode: ArgsMatch
) -> str | None:
    if mode is ArgsMatch.EXACT:
        return argument_error(definition, args)
    # Subset: only the keys given are checked; required keys left out are the agent's to add.
    partial = definition.model_copy(
        update={"inputs": tuple(f for f in definition.inputs if f.name in args)}
    )
    return argument_error(partial, args)


def contract_error(contract: CanonicalContract, body: dict[str, JsonValue]) -> str | None:
    """The first field of ``body`` that breaks the contract, or None. Names only, never values."""
    fields = {f.name: f for f in contract.fields}
    extra = sorted(set(body) - set(fields))
    if extra:
        return f"undeclared field {extra[0]}"
    for name, f in fields.items():
        if name not in body:
            return f"missing field {name}"
        if not conforms(f.type, f.values, body[name]):
            return f"field {name} is not a valid {f.type.value}"
    return None

"""Mutation canaries (brief §6.8): known-bad changes the suite must catch.

Each canary rewrites the sandbox inputs (tool definitions or fixture outputs), never the repo.
It is caught when a task that passed on the unmutated inputs fails on the mutated ones.
"""

import copy
from collections.abc import Mapping, Sequence
from datetime import datetime, timedelta
from enum import StrEnum

from pydantic import Field, JsonValue

from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.contracts import CanonicalContract, ContractType
from adapter_verify.golden.domain.lint import Document, Inputs
from adapter_verify.golden.domain.results import SuiteResults, TaskVerdict
from adapter_verify.golden.domain.tasks import GoldenTask


class CanaryKind(StrEnum):
    ENUM_SWAP = "enum_swap"
    DESCRIPTION_MISLEAD = "description_mislead"
    DROP_FIELD = "drop_field"
    TIMEZONE_SHIFT = "timezone_shift"


class CanaryStatus(StrEnum):
    CAUGHT = "caught"
    MISSED = "missed"
    NO_COVERAGE = "no_coverage"
    """No task touches what the canary changed: a blind spot by construction."""
    BASELINE_FAILING = "baseline_failing"
    """No affected task passed unmutated, so the canary can't be judged."""
    INCOMPLETE = "incomplete"
    """Nothing failed, but a mutated task was undecided (e.g. the model was unavailable)."""


class CanaryConfig(FrozenModel):
    """``golden_tasks/canaries.yaml``: the curated parts of the canaries."""

    misleading_descriptions: dict[str, str] = Field(
        default_factory=dict, description="tool@version → a subtly misleading description."
    )
    dropped_fields: dict[str, str] = Field(
        default_factory=dict, description="tool → output field the agent relies on."
    )
    timezone_shift_hours: int = Field(
        default=-10, ge=-23, le=23, description="Shift datetimes by this much, keeping 'Z'."
    )


class Mutation(FrozenModel):
    kind: CanaryKind
    inputs: Inputs
    tools: frozenset[str] = Field(description="Tools whose behavior the mutation changed.")


def _fixture_tools(tasks: Sequence[GoldenTask]) -> dict[str, str]:
    return {f.payload: f.tool for t in tasks for f in t.fixtures}


def mutate(
    kind: CanaryKind,
    config: CanaryConfig,
    inputs: Inputs,
    contracts: Mapping[str, CanonicalContract],
) -> Mutation:
    """Apply one canary. ``contracts`` is keyed by tool (its default version)."""
    if kind is CanaryKind.DESCRIPTION_MISLEAD:
        return _mislead(config, inputs)
    tasks = [d.model for d in inputs.tasks if d.model is not None]
    fixtures: dict[str, JsonValue] = copy.deepcopy(inputs.fixtures)
    changed: set[str] = set()
    for path, tool in _fixture_tools(tasks).items():
        body = fixtures.get(path)
        contract = contracts.get(tool)
        if not isinstance(body, dict) or contract is None:
            continue
        if _mutate_body(kind, config, tool, body, contract):
            changed.add(tool)
    return Mutation(
        kind=kind, inputs=inputs.model_copy(update={"fixtures": fixtures}), tools=frozenset(changed)
    )


def _mutate_body(
    kind: CanaryKind,
    config: CanaryConfig,
    tool: str,
    body: dict[str, JsonValue],
    contract: CanonicalContract,
) -> bool:
    before = copy.deepcopy(body)
    for field in contract.fields:
        value = body.get(field.name)
        swap = field.values[:2] if field.type is ContractType.ENUM and field.values else ()
        if kind is CanaryKind.ENUM_SWAP and len(swap) == 2 and value in swap:  # noqa: PLR2004
            body[field.name] = swap[1] if value == swap[0] else swap[0]
        elif (
            kind is CanaryKind.TIMEZONE_SHIFT
            and field.type is ContractType.DATETIME
            and isinstance(value, str)
        ):
            body[field.name] = _shift(value, config.timezone_shift_hours)
    if kind is CanaryKind.DROP_FIELD:
        body.pop(config.dropped_fields.get(tool, ""), None)
    return body != before


def _shift(value: str, hours: int) -> str:
    """Local wall time mislabeled as UTC: the classic timezone bug."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    return (parsed + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")


def _mislead(config: CanaryConfig, inputs: Inputs) -> Mutation:
    docs = []
    changed: set[str] = set()
    for doc in inputs.definitions:
        d = doc.model
        text = None if d is None else config.misleading_descriptions.get(d.key)
        if d is not None and text is not None and text != d.description:
            docs.append(
                Document(
                    path=doc.path, model=d.model_copy(update={"description": text}), load_error=None
                )
            )
            changed.add(d.tool)
        else:
            docs.append(doc)
    return Mutation(
        kind=CanaryKind.DESCRIPTION_MISLEAD,
        inputs=inputs.model_copy(update={"definitions": tuple(docs)}),
        tools=frozenset(changed),
    )


def affected(tasks: Sequence[GoldenTask], tools: frozenset[str]) -> list[str]:
    return sorted(t.task_id for t in tasks if t.tools() & tools)


def status(
    task_ids: Sequence[str], baseline: SuiteResults, mutated: SuiteResults | None
) -> CanaryStatus:
    if not task_ids:
        return CanaryStatus.NO_COVERAGE
    passing = [
        t for t in task_ids if (r := baseline.task(t)) is not None and r.verdict is TaskVerdict.PASS
    ]
    if not passing or mutated is None:
        return CanaryStatus.BASELINE_FAILING
    verdicts = {r.verdict for t in passing if (r := mutated.task(t)) is not None}
    if TaskVerdict.FAIL in verdicts:
        return CanaryStatus.CAUGHT
    if TaskVerdict.INCOMPLETE in verdicts:
        return CanaryStatus.INCOMPLETE
    return CanaryStatus.MISSED

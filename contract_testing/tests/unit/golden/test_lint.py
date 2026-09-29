from pathlib import Path
from typing import Any

import pytest

from adapter_verify import composition
from adapter_verify.golden.domain.lint import RULES, lint
from adapter_verify.settings import ContractSettings
from tests.unit.golden.support import Repo, read_task, write_task

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]


def _findings(repo: Repo) -> set[tuple[str, str]]:
    _, access = repo.settings()
    _, catalog = composition.load_access_config(access)
    contracts = composition.canonical_contracts(ContractSettings(root=repo.root))
    found = lint(repo.workspace().inputs, catalog, repo.policies(), contracts)
    return {(f.rule_id, f.detail) for f in found}


def _rules(repo: Repo) -> set[str]:
    return {rule for rule, _ in _findings(repo)}


def test_clean_repo_has_no_findings(tmp_path: Path) -> None:
    assert _findings(Repo.create(tmp_path)) == set()


def test_pilot_golden_tasks_are_clean() -> None:
    assert _findings(Repo(REPO_ROOT)) == set()


@pytest.mark.parametrize(
    ("task", "rule"),
    [
        (read_task(expect_calls=[{"tool": "order.list"}]), "GOLDEN_UNKNOWN_TOOL"),
        (
            read_task(
                fixtures=[
                    {
                        "tool": "order.list",
                        "payload": "fixtures/golden/order_1_inprog.synthetic.json",
                    }
                ]
            ),
            "GOLDEN_UNKNOWN_TOOL",
        ),
        (
            read_task(expect_calls=[{"tool": "order.get", "semantic_version": "9.0.0"}]),
            "GOLDEN_UNKNOWN_VERSION",
        ),
        (read_task(agent="ghost", task_id="t"), "GOLDEN_UNKNOWN_AGENT"),
        (
            read_task(
                expect_calls=[{"tool": "order.cancel", "args": {"order_id": "1"}}],
                expect_no_calls=[],
            ),
            "GOLDEN_TOOL_NOT_GRANTED",
        ),
        (read_task(expect_no_calls=["order.get"]), "GOLDEN_EXPECT_CONFLICT"),
        (
            read_task(expect_calls=[{"tool": "order.get", "args": {"id": "1"}}]),
            "GOLDEN_ARGS_INVALID",
        ),
        (
            write_task(
                expect_calls=[
                    {"tool": "order.cancel", "args": {"reason": "bored"}, "args_match": "subset"}
                ]
            ),
            "GOLDEN_ARGS_INVALID",
        ),
        (
            read_task(
                fixtures=[
                    {"tool": "order.get", "payload": "fixtures/golden/missing.synthetic.json"}
                ]
            ),
            "GOLDEN_FIXTURE_MISSING",
        ),
        (
            read_task(fixtures=[{"tool": "order.get", "payload": "fixtures/real/order.json"}]),
            "GOLDEN_FIXTURE_NOT_SYNTHETIC",
        ),
        (read_task(threshold=4), "GOLDEN_THRESHOLD"),
        (write_task(runs=3, threshold=2), "GOLDEN_THRESHOLD"),
        (read_task(expect_state={"writes_performed": 1}, runs=3, threshold=2), "GOLDEN_THRESHOLD"),
    ],
)
def test_task_rules(tmp_path: Path, task: dict[str, Any], rule: str) -> None:
    repo = Repo.create(tmp_path, tasks=[task])
    if task["agent"] == "ghost":
        repo.write("golden_tasks/ghost/agent.yaml", {"agent_id": "ghost", "harness": "scripted"})
    assert rule in _rules(repo)


def test_fixture_must_match_the_contract(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path)
    bad = {
        "order_1_inprog": {"order_id": "1", "status": "shipped"},
    }
    repo.write("fixtures/golden/order_1_inprog.synthetic.json", bad["order_1_inprog"])
    assert (
        "GOLDEN_FIXTURE_CONTRACT",
        "fixtures/golden/order_1_inprog.synthetic.json: field status is not a valid enum",
    ) in _findings(repo)
    repo.write("fixtures/golden/order_1_inprog.synthetic.json", {"order_id": "1"})
    assert any("missing field status" in d for _, d in _findings(repo))
    repo.write(
        "fixtures/golden/order_1_inprog.synthetic.json",
        {"order_id": "1", "status": "completed", "x": 1},
    )
    assert any("undeclared field x" in d for _, d in _findings(repo))
    repo.write("fixtures/golden/order_1_inprog.synthetic.json", [1, 2])
    assert "GOLDEN_FIXTURE_MISSING" in _rules(repo)


def test_file_level_rules(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path)
    repo.write("golden_tasks/reader/wrong-name.yaml", read_task())  # duplicate id, wrong name
    repo.write("golden_tasks/reader/broken.yaml", {"task_id": "broken"})
    repo.write("golden_tasks/writer/agent.yaml", {"agent_id": "reader", "harness": "scripted"})
    repo.write(
        "golden_tasks/quarantine.yaml",
        {"entries": [{"task_id": "nope", "owner": "o", "reason": "r", "since": "2026-09-26"}]},
    )
    rules = _rules(repo)
    assert {
        "GOLDEN_DUPLICATE_ID",
        "GOLDEN_FILE_NAME_MISMATCH",
        "GOLDEN_SCHEMA",
        "GOLDEN_AGENT_CONFIG_MISSING",
        "GOLDEN_QUARANTINE_UNKNOWN_TASK",
    } <= rules


def test_definition_rules(tmp_path: Path) -> None:
    repo = Repo.create(tmp_path)
    (tmp_path / "catalog/definitions/order.cancel/1.0.0.yaml").unlink()
    repo.write(
        "catalog/definitions/order.list/1.0.0.yaml",
        {"tool": "order.list", "version": "1.0.0", "description": "d"},
    )
    repo.write("catalog/definitions/order.get/bad.yaml", {"tool": "order.get"})
    findings = _findings(repo)
    assert ("GOLDEN_DEFINITION_MISSING", "order.cancel@1.0.0") in findings
    assert ("GOLDEN_DEFINITION_ORPHAN", "order.list@1.0.0") in findings
    assert "GOLDEN_SCHEMA" in _rules(repo)


def test_every_rule_is_documented() -> None:
    assert all(rule.startswith("GOLDEN_") and text for rule, text in RULES.items())

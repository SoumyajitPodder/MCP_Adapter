"""A small synthetic repository for golden tests: catalog, policies, credentials, contracts,
definitions, agent configs, tasks and fixtures. Includes a mutating tool (order.cancel) the
pilot catalog doesn't have, so write paths are exercised."""

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from adapter_verify import composition
from adapter_verify.access.domain.policy import PolicySet
from adapter_verify.common.adapters.system import SystemClock
from adapter_verify.golden.adapters.files import Workspace
from adapter_verify.golden.domain.results import SuiteResults
from adapter_verify.golden.fakes import RecordingEgressGuard
from adapter_verify.golden.ports import AgentHarness, HarnessFactory, Judge
from adapter_verify.golden.service import GoldenRunner, SuitePlan
from adapter_verify.settings import AccessSettings, GoldenSettings

CATALOG: dict[str, Any] = {
    "tools": [
        {"tool": "order.get", "version": "1.2.0", "behavior": "read_only", "default": True},
        {"tool": "order.cancel", "version": "1.0.0", "behavior": "mutating", "default": True},
    ]
}
POLICIES: dict[str, dict[str, Any]] = {
    "reader": {
        "agent_id": "reader",
        "owner": "team",
        "scope": "read",
        "grants": [{"tool": "order.get", "versions": ">=1.0.0,<2.0.0"}],
    },
    "writer": {
        "agent_id": "writer",
        "owner": "team",
        "scope": "write",
        "grants": [
            {"tool": "order.get", "versions": ">=1.0.0,<2.0.0"},
            {"tool": "order.cancel", "versions": ">=1.0.0,<2.0.0"},
        ],
    },
}
CREDENTIALS: dict[str, Any] = {
    "bindings": [
        {"tool": "order.get", "versions": ">=1.0.0,<2.0.0", "secret_name": "orders/read"},
        {"tool": "order.cancel", "versions": ">=1.0.0,<2.0.0", "secret_name": "orders/write"},
    ]
}
CONTRACTS: dict[str, dict[str, Any]] = {
    "order.get/1.2.0": {
        "tool": "order.get",
        "version": "1.2.0",
        "fields": [
            {"name": "order_id", "type": "string"},
            {"name": "status", "type": "enum", "values": ["in_progress", "completed", "cancelled"]},
        ],
    },
    "order.cancel/1.0.0": {
        "tool": "order.cancel",
        "version": "1.0.0",
        "fields": [
            {"name": "order_id", "type": "string"},
            {"name": "cancelled", "type": "boolean"},
        ],
    },
}
DEFINITIONS: dict[str, dict[str, Any]] = {
    "order.get/1.2.0": {
        "tool": "order.get",
        "version": "1.2.0",
        "description": "Look up one order by ID. Returns its status.",
        "inputs": [{"name": "order_id", "type": "string"}],
    },
    "order.cancel/1.0.0": {
        "tool": "order.cancel",
        "version": "1.0.0",
        "description": "Cancel one order. Changes state.",
        "inputs": [
            {"name": "order_id", "type": "string"},
            {"name": "reason", "type": "enum", "values": ["customer", "fraud"], "required": False},
        ],
    },
}
FIXTURES: dict[str, dict[str, Any]] = {
    "order_1_inprog": {"order_id": "1", "status": "in_progress"},
    "order_1_cancel": {"order_id": "1", "cancelled": True},
}


def read_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "order-inflight",
        "agent": "reader",
        "description": "reports an in-progress order",
        "prompt": "Is order 1 shipped?",
        "fixtures": [
            {"tool": "order.get", "payload": "fixtures/golden/order_1_inprog.synthetic.json"}
        ],
        "expect_calls": [{"tool": "order.get", "args": {"order_id": "1"}}],
        "expect_no_calls": ["order.cancel"],
        "expect_state": {"writes_performed": 0},
        "runs": 3,
        "threshold": 2,
    }
    task.update(overrides)
    return task


def write_task(**overrides: Any) -> dict[str, Any]:
    task: dict[str, Any] = {
        "task_id": "order-cancel",
        "agent": "writer",
        "description": "cancels an order once",
        "prompt": "Cancel order 1.",
        "fixtures": [
            {"tool": "order.cancel", "payload": "fixtures/golden/order_1_cancel.synthetic.json"}
        ],
        "expect_calls": [
            {"tool": "order.cancel", "args": {"order_id": "1"}, "args_match": "subset"}
        ],
        "expect_state": {"writes_performed": 1, "calls_by_tool": {"order.cancel": 1}},
        "runs": 1,
        "threshold": 1,
    }
    task.update(overrides)
    return task


def _dump(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = (
        json.dumps(data, indent=2)
        if path.suffix == ".json"
        else yaml.safe_dump(data, sort_keys=False)
    )
    path.write_text(text, encoding="utf-8", newline="\n")


@dataclass
class Repo:
    root: Path

    @classmethod
    def create(cls, root: Path, tasks: list[dict[str, Any]] | None = None) -> "Repo":
        repo = cls(root)
        _dump(root / "catalog/tools.yaml", CATALOG)
        for name, policy in POLICIES.items():
            _dump(root / f"policies/{name}.yaml", policy)
        _dump(root / "credentials.yaml", CREDENTIALS)
        for key, contract in CONTRACTS.items():
            _dump(root / f"contracts/{key}.yaml", contract)
        for key, definition in DEFINITIONS.items():
            _dump(root / f"catalog/definitions/{key}.yaml", definition)
        for agent in POLICIES:
            _dump(
                root / f"golden_tasks/{agent}/agent.yaml",
                {"agent_id": agent, "harness": "scripted", "limits": {"max_tool_calls": 5}},
            )
        for name, body in FIXTURES.items():
            _dump(root / f"fixtures/golden/{name}.synthetic.json", body)
        for task in tasks if tasks is not None else [read_task(), write_task()]:
            repo.put_task(task)
        return repo

    def put_task(self, task: dict[str, Any]) -> None:
        _dump(self.root / f"golden_tasks/{task['agent']}/{task['task_id']}.yaml", task)

    def write(self, relative: str, data: Any) -> None:
        _dump(self.root / relative, data)

    def settings(self) -> tuple[GoldenSettings, AccessSettings]:
        return GoldenSettings(root=self.root), AccessSettings(
            policies_dir=self.root / "policies",
            catalog_path=self.root / "catalog/tools.yaml",
            credentials_path=self.root / "credentials.yaml",
        )

    def workspace(self) -> Workspace:
        golden, _ = self.settings()
        return composition.golden_workspace(golden)

    def policies(self) -> PolicySet:
        _, access = self.settings()
        documents, _ = composition.load_access_config(access)
        return PolicySet(d.policy for d in documents if d.policy is not None)

    def runner(
        self,
        harness: AgentHarness,
        *,
        judge: Judge | None = None,
        calibration: list[Any] | None = None,
        egress: RecordingEgressGuard | None = None,
    ) -> GoldenRunner:
        _, access = self.settings()
        factory: HarnessFactory = lambda _config: harness  # noqa: E731
        return GoldenRunner(
            sandboxes=composition.golden_sandboxes(access, self.workspace()),
            harnesses=factory,
            judge=judge,
            calibration=calibration or [],
            egress=egress or RecordingEgressGuard(),
            clock=SystemClock(),
        )

    def plan(self, **overrides: Any) -> SuitePlan:
        ws = self.workspace()
        fields: dict[str, Any] = {
            "run_id": "golden-test",
            "git_sha": None,
            "tasks": ws.tasks,
            "agents": ws.agents,
            "digests": composition.golden_digests(ws, self.policies()),
            "quarantine": ws.quarantine,
        }
        fields.update(overrides)
        return SuitePlan(**fields)


async def run(
    repo: Repo, harness: AgentHarness, task_ids: list[str] | None = None, **kw: Any
) -> SuiteResults:
    plan_kw = {k: kw.pop(k) for k in ("previous", "token_budget") if k in kw}
    plan = repo.plan(**plan_kw)
    if task_ids is not None:
        plan = repo.plan(tasks=[t for t in plan.tasks if t.task_id in task_ids], **plan_kw)
    return await repo.runner(harness, **kw).run(plan)

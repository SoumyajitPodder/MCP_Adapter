"""Golden workspace on disk: task files, agent configs, tool definitions, fixtures, quarantine,
calibration cases and result files. Every path is repo-relative and may not escape the root."""

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from pydantic import JsonValue, ValidationError

from adapter_kernel.jsontypes import JsonObject
from adapter_verify.common.adapters.yamlfile import ConfigFileError, parse_yaml_model
from adapter_verify.golden.domain.canaries import CanaryConfig
from adapter_verify.golden.domain.definitions import ToolDefinition
from adapter_verify.golden.domain.judge import CalibrationCase
from adapter_verify.golden.domain.lint import Document, Inputs
from adapter_verify.golden.domain.results import SuiteResults
from adapter_verify.golden.domain.tasks import AgentConfig, GoldenTask, QuarantineList

AGENT_FILE = "agent.yaml"
QUARANTINE_FILE = "quarantine.yaml"
CANARIES_FILE = "canaries.yaml"


@dataclass(frozen=True)
class Workspace:
    """Everything loaded, plus the digests of fixture files."""

    inputs: Inputs
    fixture_digests: dict[str, str]

    @property
    def tasks(self) -> list[GoldenTask]:
        return [d.model for d in self.inputs.tasks if d.model is not None]

    @property
    def agents(self) -> dict[str, AgentConfig]:
        return {d.model.agent_id: d.model for d in self.inputs.agents if d.model is not None}

    @property
    def definitions(self) -> dict[str, ToolDefinition]:
        return {d.model.key: d.model for d in self.inputs.definitions if d.model is not None}

    @property
    def quarantine(self) -> QuarantineList:
        return self.inputs.quarantine.model or QuarantineList()

    def fixture(self, path: str) -> JsonObject | None:
        body = self.inputs.fixtures.get(path)
        return body if isinstance(body, dict) else None


def _load[M](root: Path, path: Path, model: type[M]) -> Document[M]:
    relative = path.relative_to(root).as_posix()
    try:
        parsed = parse_yaml_model(model, path.read_text(encoding="utf-8"))  # type: ignore[type-var]
    except (ConfigFileError, OSError) as exc:
        return Document[M](path=relative, model=None, load_error=str(exc))
    return Document[M](path=relative, model=parsed, load_error=None)


def load_workspace(root: Path, tasks_dir: Path, definitions_dir: Path) -> Workspace:
    root = root.resolve()
    tasks_path, definitions_path = root / tasks_dir, root / definitions_dir
    task_docs, agent_docs = [], []
    for path in sorted(tasks_path.glob("*/*.yaml")):
        if path.name == AGENT_FILE:
            agent_docs.append(_load(root, path, AgentConfig))
        else:
            task_docs.append(_load(root, path, GoldenTask))
    quarantine_path = tasks_path / QUARANTINE_FILE
    quarantine = (
        _load(root, quarantine_path, QuarantineList)
        if quarantine_path.exists()
        else Document[QuarantineList](
            path=quarantine_path.relative_to(root).as_posix(),
            model=QuarantineList(),
            load_error=None,
        )
    )
    definition_docs = [
        _load(root, p, ToolDefinition) for p in sorted(definitions_path.glob("*/*.yaml"))
    ]
    fixtures: dict[str, JsonValue] = {}
    digests: dict[str, str] = {}
    for doc in task_docs:
        for ref in doc.model.fixtures if doc.model else ():
            if ref.payload in fixtures:
                continue
            raw = _read_inside(root, ref.payload)
            if raw is None:
                continue
            digests[ref.payload] = hashlib.sha256(raw).hexdigest()
            try:
                fixtures[ref.payload] = json.loads(raw)
            except ValueError:
                continue
    return Workspace(
        inputs=Inputs(
            tasks=tuple(task_docs),
            agents=tuple(agent_docs),
            definitions=tuple(definition_docs),
            quarantine=quarantine,
            fixtures=fixtures,
            tasks_dir=tasks_path.relative_to(root).as_posix(),
            definitions_dir=definitions_path.relative_to(root).as_posix(),
        ),
        fixture_digests=digests,
    )


def _read_inside(root: Path, relative: str) -> bytes | None:
    path = (root / relative).resolve()
    if not path.is_relative_to(root) or not path.is_file():
        return None
    return path.read_bytes()


def load_canaries(tasks_dir: Path) -> CanaryConfig:
    path = tasks_dir / CANARIES_FILE
    if not path.exists():
        return CanaryConfig()
    return parse_yaml_model(CanaryConfig, path.read_text(encoding="utf-8"))


def with_inputs(workspace: Workspace, inputs: Inputs) -> Workspace:
    """The same workspace with mutated inputs (canaries); fixture digests stay the originals."""
    return Workspace(inputs=inputs, fixture_digests=workspace.fixture_digests)


def load_calibration(directory: Path) -> list[CalibrationCase]:
    """Calibration cases (§6.5). An invalid case is an error: calibration must be trustworthy."""
    cases = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            cases.append(parse_yaml_model(CalibrationCase, path.read_text(encoding="utf-8")))
        except ConfigFileError as exc:
            msg = f"{path.name}: {exc}"
            raise ConfigFileError(msg) from None
    return cases


def write_results(results: SuiteResults, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(results.model_dump(mode="json"), indent=2, sort_keys=True) + "\n"
    path.write_text(body, encoding="utf-8", newline="\n")


def read_results(path: Path) -> SuiteResults:
    try:
        return SuiteResults.model_validate_json(path.read_text(encoding="utf-8"))
    except (OSError, ValidationError) as exc:
        msg = f"{path.name}: not a readable results file ({type(exc).__name__})"
        raise ConfigFileError(msg) from None

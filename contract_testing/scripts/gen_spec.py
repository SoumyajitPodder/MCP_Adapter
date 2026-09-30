"""Regenerate the generated sections of docs/SPEC.md and the JSON Schemas in docs/schemas/.

Usage:
    uv run python scripts/gen_spec.py           # rewrite in place
    uv run python scripts/gen_spec.py --check   # exit 1 if anything is stale (CI)

Hand-written text outside the BEGIN/END GENERATED markers is never touched.
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from enum import StrEnum
from pathlib import Path

import click
from pydantic import BaseModel
from pydantic_settings import BaseSettings

from adapter_kernel.classification import Classification
from adapter_kernel.context import CallContext, RequestContext
from adapter_kernel.errors import ERROR_SPECS, AdapterError, ErrorSpec, RetryPolicy
from adapter_kernel.meta import ResponseMeta
from adapter_kernel.pipeline import (
    DeliveryStatus,
    ToolFailure,
    ToolRequest,
    ToolResult,
    ToolSuccess,
)
from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)
from adapter_kernel.tooldef import Behavior, Sensitivity
from adapter_verify.access.domain import evaluate as access_rules
from adapter_verify.access.domain.credentials import CredentialBinding, CredentialMap
from adapter_verify.access.domain.lint import RULES as POLICY_LINT_RULES
from adapter_verify.access.domain.policy import CatalogEntry, Grant, Policy, ToolCatalog
from adapter_verify.cli.main import cli
from adapter_verify.contract_ci.adapters.files import load_rulebook
from adapter_verify.contract_ci.domain import checks as contract_checks
from adapter_verify.contract_ci.domain.contracts import CanonicalContract, ReleaseLock
from adapter_verify.contract_ci.domain.sources import SourceConfig
from adapter_verify.golden.domain import lint as golden_lint
from adapter_verify.golden.domain.canaries import CanaryConfig
from adapter_verify.golden.domain.definitions import InputField, ToolDefinition
from adapter_verify.golden.domain.judge import CalibrationCase
from adapter_verify.golden.domain.results import RESULTS_SCHEMA_VERSION, SuiteResults
from adapter_verify.golden.domain.tasks import (
    AgentConfig,
    ExpectedAnswer,
    ExpectedCall,
    ExpectedState,
    FixtureRef,
    GoldenTask,
    QuarantineEntry,
    QuarantineList,
    RunLimits,
    Waiver,
)
from adapter_verify.idempotency.domain.records import AlertKind, IdempotencyRecord
from adapter_verify.observability.domain.attributes import (
    SEMCONV_GENAI_COMMIT,
    SpanAttributes,
    SpanName,
)
from adapter_verify.observability.domain.audit import AuditEvent, AuditRecord
from adapter_verify.observability.domain.events import EVENT_SCHEMA_VERSION, CallEvent
from adapter_verify.settings import (
    AccessSettings,
    ContractSettings,
    DatabaseSettings,
    GoldenSettings,
    IdempotencySettings,
    ObservabilitySettings,
)

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "docs" / "SPEC.md"
SCHEMAS = ROOT / "docs" / "schemas"

KERNEL_MODELS: tuple[type[BaseModel], ...] = (
    RequestContext,
    CallContext,
    ToolRequest,
    ToolResult,
    ToolSuccess,
    ToolFailure,
    ResponseMeta,
    AdapterError,
    ErrorSpec,
    Shape,
    OperationShape,
    FieldShape,
    Provenance,
)
SCHEMA_MODELS: tuple[type[BaseModel], ...] = (
    *KERNEL_MODELS,
    CallEvent,
    SpanAttributes,
    AuditEvent,
    AuditRecord,
    Policy,
    ToolCatalog,
    CredentialMap,
    SourceConfig,
    CanonicalContract,
    ReleaseLock,
    IdempotencyRecord,
    GoldenTask,
    AgentConfig,
    QuarantineList,
    ToolDefinition,
    CalibrationCase,
    SuiteResults,
    CanaryConfig,
)
SETTINGS: tuple[type[BaseSettings], ...] = (
    ObservabilitySettings,
    DatabaseSettings,
    AccessSettings,
    IdempotencySettings,
    ContractSettings,
    GoldenSettings,
)
KERNEL_ENUMS: tuple[type[StrEnum], ...] = (
    Classification,
    Behavior,
    Sensitivity,
    DeliveryStatus,
    RetryPolicy,
    FieldType,
    SourceKind,
    ProvenanceKind,
)

_QUALIFIED = re.compile(r"\b(?:[A-Za-z_]\w*\.)+([A-Za-z_]\w*)")
_MARKER = re.compile(
    r"(<!-- BEGIN GENERATED: (?P<name>[\w-]+) -->\n)(?P<body>.*?)(<!-- END GENERATED -->)",
    re.DOTALL,
)


def _default(value: object) -> str:
    """A default as it would be written in config. Paths are POSIX, so the output doesn't depend
    on the OS that generated it (repr() gives WindowsPath or PosixPath)."""
    return value.as_posix() if isinstance(value, Path) else repr(value)


def _type_name(annotation: object) -> str:
    text = annotation.__name__ if isinstance(annotation, type) else repr(annotation)
    return _QUALIFIED.sub(r"\1", text).replace("|", "\\|")


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _summary(doc: str | None) -> str:
    return _cell((doc or "").strip().split("\n\n")[0])


def _model_table(model: type[BaseModel]) -> str:
    rows = [
        f"#### `{model.__name__}`",
        "",
        _summary(model.__doc__),
        "",
        "| Field | Type | Required | Meaning |",
        "| --- | --- | --- | --- |",
    ]
    for name, info in model.model_fields.items():
        required = "yes" if info.is_required() else "no"
        rows.append(
            f"| `{name}` | `{_type_name(info.annotation)}` | {required} "
            f"| {_cell(info.description or '')} |"
        )
    return "\n".join(rows)


def _enum_table(enum: type[StrEnum]) -> str:
    values = ", ".join(f"`{member.value}`" for member in enum)
    return f"| `{enum.__name__}` | {values} | {_summary(enum.__doc__)} |"


def kernel_models() -> str:
    parts = [
        "JSON Schemas for every model are in `docs/schemas/`.",
        "",
        "### Enums",
        "",
        "| Enum | Values | Meaning |",
        "| --- | --- | --- |",
        *(_enum_table(e) for e in KERNEL_ENUMS),
        "",
        "### Models",
        "",
    ]
    parts.append("\n\n".join(_model_table(m) for m in KERNEL_MODELS))
    return "\n".join(parts)


def error_codes() -> str:
    rows = ["| Code | Retry | Raised by | Agent-facing message |", "| --- | --- | --- | --- |"]
    rows.extend(
        f"| `{code}` | `{spec.retry}` | {spec.owner} | {_cell(spec.agent_message)} |"
        for code, spec in ERROR_SPECS.items()
    )
    return "\n".join(rows)


def config() -> str:
    rows = [
        "| Environment variable | Type | Default | Required | Meaning |",
        "| --- | --- | --- | --- | --- |",
    ]
    for settings in SETTINGS:
        prefix = settings.model_config.get("env_prefix", "")
        for name, info in settings.model_fields.items():
            default = (
                "—"
                if info.is_required()
                else f"`{_default(info.get_default(call_default_factory=True))}`"
            )
            rows.append(
                f"| `{prefix}{name.upper()}` | `{_type_name(info.annotation)}` | {default} "
                f"| {'yes' if info.is_required() else 'no'} | {_cell(info.description or '')} |"
            )
    return "\n".join(rows)


def event_schema() -> str:
    return "\n\n".join(
        [
            f"Current `schema_version`: **{EVENT_SCHEMA_VERSION}**. "
            "JSON Schema: `docs/schemas/CallEvent.json`.",
            _model_table(CallEvent),
            "Span names: " + ", ".join(f"`{s.value}`" for s in SpanName) + ".",
            _model_table(SpanAttributes),
            f"Mirrored semconv names are pinned to `semantic-conventions-genai@"
            f"{SEMCONV_GENAI_COMMIT[:12]}`.",
        ]
    )


def _commands(group: click.Group, prefix: str) -> list[tuple[str, click.Command]]:
    found: list[tuple[str, click.Command]] = []
    for name in sorted(group.commands):
        command = group.commands[name]
        path = f"{prefix} {name}"
        if isinstance(command, click.Group):
            found.extend(_commands(command, path))
        else:
            found.append((path, command))
    return found


def cli_reference() -> str:
    parts = [
        "Exit codes: `0` success, `1` check failed or nothing found, `3` configuration or "
        "tool error.",
    ]
    for path, command in _commands(cli, "adapter-verify"):
        rows = [
            f"#### `{path}`",
            "",
            _summary(command.help),
            "",
            "| Option | Type | Default | Required | Meaning |",
            "| --- | --- | --- | --- | --- |",
        ]
        options = [p for p in command.params if isinstance(p, click.Option)]
        for option in options:
            default = option.default if option.show_default else None
            if isinstance(default, Path):
                default = default.relative_to(ROOT).as_posix()
            rows.append(
                f"| `{'`, `'.join(option.opts)}` | `{option.type.name}` "
                f"| {'—' if default is None else f'`{default}`'} "
                f"| {'yes' if option.required else 'no'} | {_cell(option.help or '')} |"
            )
        if not options:
            rows.append("| — | | | | no options |")
        parts.append("\n".join(rows))
    return "\n\n".join(parts)


def policy_schema() -> str:
    decision_rules = sorted(
        str(value)
        for name, value in vars(access_rules).items()
        if name.startswith("RULE_") and isinstance(value, str)
    )
    lint_rows = "\n".join(
        f"| `{rule}` | {_cell(text)} |" for rule, text in POLICY_LINT_RULES.items()
    )
    return "\n\n".join(
        [
            "JSON Schemas: `docs/schemas/Policy.json`, `ToolCatalog.json`, `CredentialMap.json`.",
            _model_table(Policy),
            _model_table(Grant),
            _model_table(CatalogEntry),
            _model_table(CredentialBinding),
            "**Decision rule IDs:** "
            + ", ".join(f"`{r}`" for r in decision_rules)
            + " (`ACCESS_GRANTED:<agent>/<tool>/<grant-index>` when allowed).",
            "| Lint rule | Fails when |\n| --- | --- |\n" + lint_rows,
        ]
    )


def contract_rules() -> str:
    rows = [
        "| Rule | Direction | Upstream | Canonical | Detects |",
        "| --- | --- | --- | --- | --- |",
    ]
    for r in load_rulebook().rules:
        flags = [
            f
            for f, on in (
                ("presence", r.presence),
                ("MINOR-bump COMPATIBLE", r.compatible_with_minor_bump),
            )
            if on
        ]
        extra = f" ({', '.join(flags)})" if flags else ""
        rows.append(
            f"| `{r.id}` | {r.direction.value} | {r.upstream.value if r.upstream else '-'} "
            f"| {r.canonical.value if r.canonical else '-'} | {_cell(r.detects)}{extra} |"
        )
    gate_rules = sorted(
        str(v)
        for k, v in vars(contract_checks).items()
        if k.startswith("RULE_") and isinstance(v, str)
    )
    return (
        "\n".join(rows)
        + "\n\n**Check-level rule IDs:** "
        + ", ".join(f"`{r}`" for r in gate_rules)
        + "."
    )


def idempotency_record() -> str:
    alerts = ", ".join(f"`{a.value}`" for a in AlertKind)
    return "\n\n".join(
        [
            "JSON Schema: `docs/schemas/IdempotencyRecord.json`.",
            _model_table(IdempotencyRecord),
            f"Owner alert kinds: {alerts}.",
        ]
    )


def golden_task_schema() -> str:
    rules = "\n".join(f"| `{rule}` | {_cell(text)} |" for rule, text in golden_lint.RULES.items())
    return "\n\n".join(
        [
            "JSON Schemas: `docs/schemas/GoldenTask.json`, `AgentConfig.json`, "
            "`QuarantineList.json`, `ToolDefinition.json`, `CalibrationCase.json`, "
            f"`SuiteResults.json` (results schema version {RESULTS_SCHEMA_VERSION}), "
            "`CanaryConfig.json`.",
            *(
                _model_table(m)
                for m in (
                    GoldenTask,
                    FixtureRef,
                    ExpectedCall,
                    ExpectedState,
                    ExpectedAnswer,
                    AgentConfig,
                    RunLimits,
                    QuarantineEntry,
                    Waiver,
                    ToolDefinition,
                    InputField,
                    CanaryConfig,
                )
            ),
            "| Lint rule | Fails when |\n| --- | --- |\n" + rules,
        ]
    )


def _not_yet(milestone: str) -> Callable[[], str]:
    return lambda: f"_Not implemented yet ({milestone})._"


SECTIONS: dict[str, Callable[[], str]] = {
    "kernel-models": kernel_models,
    "error-codes": error_codes,
    "config": config,
    "rules": contract_rules,
    "golden-task-schema": golden_task_schema,
    "idempotency-record": idempotency_record,
    "event-schema": event_schema,
    "policy-schema": policy_schema,
    "cli": cli_reference,
}


def render_spec(current: str) -> str:
    seen: set[str] = set()

    def replace(match: re.Match[str]) -> str:
        name = match.group("name")
        if name not in SECTIONS:
            msg = f"SPEC.md has a generated section with no generator: {name}"
            raise KeyError(msg)
        seen.add(name)
        return f"{match.group(1)}{SECTIONS[name]()}\n{match.group(4)}"

    rendered = _MARKER.sub(replace, current)
    missing = set(SECTIONS) - seen
    if missing:
        msg = f"SPEC.md is missing generated sections: {sorted(missing)}"
        raise KeyError(msg)
    return rendered


def render_schemas() -> dict[Path, str]:
    return {
        SCHEMAS / f"{model.__name__}.json": json.dumps(
            model.model_json_schema(), indent=2, sort_keys=True, ensure_ascii=False
        )
        + "\n"
        for model in SCHEMA_MODELS
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--check", action="store_true", help="fail if anything is stale")
    check = parser.parse_args().check

    wanted = {SPEC: render_spec(SPEC.read_text(encoding="utf-8")), **render_schemas()}
    stale_extra = {p for p in SCHEMAS.glob("*.json") if p not in wanted}
    stale = [
        p for p, text in wanted.items() if not p.exists() or p.read_text(encoding="utf-8") != text
    ]

    if check:
        for path in [*stale, *sorted(stale_extra)]:
            print(f"stale: {path.relative_to(ROOT)}")
        if stale or stale_extra:
            print("run: uv run python scripts/gen_spec.py")
            return 1
        return 0

    SCHEMAS.mkdir(parents=True, exist_ok=True)
    for path in stale:
        path.write_text(wanted[path], encoding="utf-8", newline="\n")
    for path in stale_extra:
        path.unlink()
    return 0


if __name__ == "__main__":
    sys.exit(main())

import json
from pathlib import Path

import pytest
from click.testing import CliRunner
from pydantic import ValidationError

from adapter_kernel.shape import FieldType, SourceKind
from adapter_verify.cli.main import cli
from adapter_verify.contract_ci.adapters.files import load_rulebook
from adapter_verify.contract_ci.domain.extract import ColumnSpec
from adapter_verify.contract_ci.domain.rules import RuleBook
from adapter_verify.contract_ci.domain.sources import SampleFile, SourceConfig

pytestmark = pytest.mark.unit

SOURCE = "order-management.rest.get-order"


def test_rules_list_prints_every_rule() -> None:
    result = CliRunner().invoke(cli, ["rules", "list"])
    assert result.exit_code == 0
    assert len(result.output.splitlines()) == len(load_rulebook().rules)


@pytest.mark.usefixtures("workspace")
def test_baseline_extract_and_accept() -> None:
    extracted = CliRunner().invoke(cli, ["baseline", "extract", "--source", SOURCE])
    assert extracted.exit_code == 0
    assert json.loads(extracted.output)["source_id"] == SOURCE
    accepted = CliRunner().invoke(cli, ["baseline", "accept", "--source", SOURCE])
    assert accepted.exit_code == 0
    assert accepted.output.strip() == f"baselines/{SOURCE}/v2.shape.json"
    for args in (
        ["baseline", "extract", "--source", "nope"],
        ["baseline", "accept", "--source", "nope"],
    ):
        assert CliRunner().invoke(cli, args).exit_code == 3


def test_release_is_idempotent_and_refuses_changed_content(workspace: Path) -> None:
    runner = CliRunner()
    assert (
        runner.invoke(
            cli, ["contract", "release", "--tool", "order.get", "--version", "1.2.0"]
        ).exit_code
        == 0
    )
    assert (
        runner.invoke(
            cli, ["contract", "release", "--tool", "order.get", "--version", "9.9.9"]
        ).exit_code
        == 3
    )
    path = workspace / "contracts" / "order.get" / "1.2.0.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("cancelled]", "cancelled, held]"), encoding="utf-8"
    )
    result = runner.invoke(
        cli, ["contract", "release", "--tool", "order.get", "--version", "1.2.0"]
    )
    assert result.exit_code == 3
    assert "different content" in result.output


def test_contract_diff_classifies_two_shape_files(workspace: Path) -> None:
    base = workspace / "baselines" / SOURCE / "v2.shape.json"
    body = json.loads(base.read_text(encoding="utf-8"))
    body.pop("content_hash")
    body["operations"][0]["outputs"] = [
        f for f in body["operations"][0]["outputs"] if f["path"] != "orderId"
    ]
    rev = workspace / "rev.json"
    rev.write_text(json.dumps(body), encoding="utf-8")
    result = CliRunner().invoke(cli, ["contract", "diff", "--base", str(base), "--rev", str(rev)])
    assert result.exit_code == 1
    assert "OUTPUT_FIELD_REMOVED" in result.output
    same = CliRunner().invoke(cli, ["contract", "diff", "--base", str(base), "--rev", str(base)])
    assert (same.exit_code, same.output.strip()) == (0, "overall: COMPATIBLE")


@pytest.mark.usefixtures("workspace")
def test_invalid_contract_settings_are_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_CONTRACT_RENAME_THRESHOLD", "2")
    assert CliRunner().invoke(cli, ["contract", "check"]).exit_code == 3


def _source(kind: SourceKind, layout: tuple[ColumnSpec, ...] | None = None) -> SourceConfig:
    return SourceConfig(
        source_id="s",
        kind=kind,
        version="v1",
        operation="op",
        tools=("t",),
        samples=(SampleFile(path="x.jsonl"),),
        layout=layout,
    )


def test_source_config_rules() -> None:
    layout = (ColumnSpec(name="a", type=FieldType.STRING),)
    assert _source(SourceKind.OBSERVED).layout is None
    with pytest.raises(ValidationError, match="no declared layout"):
        _source(SourceKind.OBSERVED, layout)
    with pytest.raises(ValidationError, match="need a declared layout"):
        _source(SourceKind.FILE)
    with pytest.raises(ValidationError, match="captured_at"):
        _source(SourceKind.FILE, layout)
    with pytest.raises(ValidationError, match="Phase 1"):
        _source(SourceKind.OPENAPI)


def test_rulebook_rejects_duplicates_and_unknown_ids() -> None:
    book = load_rulebook()
    with pytest.raises(ValidationError, match="duplicate rule"):
        RuleBook(rules=(book.rules[0], book.rules[0]))
    with pytest.raises(KeyError, match="unknown rule"):
        book.get("NOPE")

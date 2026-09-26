from pathlib import Path

import pytest
from click.testing import CliRunner

from adapter_verify.cli.main import cli

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def repo_config(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_ACCESS_POLICIES_DIR", str(REPO / "policies"))
    monkeypatch.setenv("ADAPTER_ACCESS_CATALOG_PATH", str(REPO / "catalog" / "tools.yaml"))


@pytest.mark.usefixtures("repo_config")
def test_lint_passes_on_repository_policies() -> None:
    result = CliRunner().invoke(cli, ["policy", "lint"])
    assert result.exit_code == 0, result.output
    assert "clean" in result.output


def test_lint_reports_findings_and_exits_1(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "a.yaml").write_text(
        "agent_id: b\nowner: t\nscope: read\ngrants:\n  - tool: order.*\n    versions: '>=1.0.0'\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("ADAPTER_ACCESS_POLICIES_DIR", str(tmp_path))
    monkeypatch.setenv("ADAPTER_ACCESS_CATALOG_PATH", str(REPO / "catalog" / "tools.yaml"))
    result = CliRunner().invoke(cli, ["policy", "lint"])
    assert result.exit_code == 1
    assert "POLICY_FILE_NAME_MISMATCH" in result.output
    assert "POLICY_WILDCARD" in result.output


@pytest.mark.usefixtures("repo_config")
@pytest.mark.parametrize(
    ("args", "code", "expected"),
    [
        (["--agent", "order-status-agent", "--tool", "order.get"], 0, "ACCESS_GRANTED"),
        (
            ["--agent", "order-status-agent", "--tool", "inventory.snapshot"],
            1,
            "ACCESS_TOOL_NOT_GRANTED",
        ),
        (["--agent", "nobody", "--tool", "order.get"], 1, "ACCESS_NO_POLICY"),
        (
            ["--agent", "order-status-agent", "--tool", "order.get", "--version", "9.0.0"],
            1,
            "ACCESS_UNKNOWN_TOOL",
        ),
    ],
)
def test_explain(args: list[str], code: int, expected: str) -> None:
    result = CliRunner().invoke(cli, ["access", "explain", *args])
    assert result.exit_code == code
    assert expected in result.output


def test_missing_catalog_is_a_tool_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_ACCESS_CATALOG_PATH", str(tmp_path / "missing.yaml"))
    result = CliRunner().invoke(cli, ["policy", "lint"])
    assert result.exit_code == 3


def test_invalid_access_settings_are_a_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ADAPTER_ACCESS_CLOCK_SKEW_S", "9999")
    result = CliRunner().invoke(cli, ["policy", "lint"])
    assert result.exit_code == 3
    assert "clock_skew_s" in result.output


def test_explain_refuses_unlinted_policies(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "x.yaml").write_text("not: a policy\n", encoding="utf-8")
    monkeypatch.setenv("ADAPTER_ACCESS_POLICIES_DIR", str(tmp_path))
    monkeypatch.setenv("ADAPTER_ACCESS_CATALOG_PATH", str(REPO / "catalog" / "tools.yaml"))
    result = CliRunner().invoke(cli, ["access", "explain", "--agent", "x", "--tool", "order.get"])
    assert result.exit_code == 3

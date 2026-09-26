"""Brief §5.11 acceptance: a deliberately breaking change in each pilot source kind is blocked
with the correct rule ID, and the impact report names the right agents. Runs the real CLI
against a copy of the repository's contract, access and sample files."""

from collections.abc import Callable
from pathlib import Path

import pytest
from click.testing import CliRunner

from adapter_verify.cli.main import cli

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]
ORDERS = "fixtures/samples/order-management.get-order.synthetic.jsonl"
FEED = "fixtures/samples/inventory-feed.2026-09-02.synthetic.csv"


def _edit(path: Path, change: Callable[[str], str]) -> None:
    path.write_text(change(path.read_text(encoding="utf-8")), encoding="utf-8", newline="\n")


@pytest.mark.usefixtures("workspace")
def test_unchanged_repository_passes() -> None:
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 0, result.output


def test_rest_field_removal_is_blocked_and_names_callers(workspace: Path) -> None:
    _edit(workspace / ORDERS, lambda t: t.replace('"customer": {', '"client": {'))
    report = workspace / "report.md"
    result = CliRunner().invoke(cli, ["contract", "check", "--report", str(report)])
    assert result.exit_code == 1
    text = report.read_text(encoding="utf-8")
    assert "`OUTPUT_FIELD_REMOVED`" in text
    assert "`customer`" in text
    assert "Agents to retest: `order-status-agent`." in text


def test_rest_new_enum_value_needs_review(workspace: Path) -> None:
    _edit(
        workspace / ORDERS,
        lambda t: t.replace('"orderState": "INPROG"', '"orderState": "WAITING_APPROVAL"', 1),
    )
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 2
    assert "OUTPUT_ENUM_VALUE_ADDED" in result.output
    assert CliRunner().invoke(cli, ["contract", "check", "--review-approved"]).exit_code == 0


def test_rest_date_format_flip_needs_review(workspace: Path) -> None:
    import re  # noqa: PLC0415

    _edit(
        workspace / ORDERS,
        lambda t: re.sub(
            r'"createdDate": "(\d{2})/(\d{2})/(\d{4}) (\d{2}:\d{2}:\d{2})"',
            r'"createdDate": "\3-\1-\2T\4Z"',
            t,
        ),
    )
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 2
    assert "FORMAT_CHANGED" in result.output


def test_csv_column_removal_is_blocked(workspace: Path) -> None:
    def drop_site(text: str) -> str:
        rows = [line.split(",") for line in text.strip().splitlines()]
        return "\n".join(",".join(r[:2] + r[3:]) for r in rows) + "\n"

    for csv_file in (workspace / "fixtures" / "samples").glob("inventory-feed.*.csv"):
        _edit(csv_file, drop_site)
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 1
    assert "OUTPUT_FIELD_REMOVED" in result.output
    assert "`site_id`" in result.output


def test_csv_delimiter_change_is_unknown(workspace: Path) -> None:
    _edit(workspace / FEED, lambda t: t.replace(",", ";"))
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 3
    assert "SOURCE_UNPARSEABLE" in result.output


def test_editing_a_released_contract_is_blocked(workspace: Path) -> None:
    _edit(
        workspace / "contracts" / "order.get" / "1.2.0.yaml",
        lambda t: t.replace("cancelled]", "cancelled, held]"),
    )
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 1
    assert "CONTRACT_RELEASED_VERSION_EDITED" in result.output


def test_hand_edited_baseline_is_rejected(workspace: Path) -> None:
    _edit(
        workspace / "baselines" / "order-management.rest.get-order" / "v2.shape.json",
        lambda t: t.replace('"INPROG"', '"IN_PROGRESS"'),
    )
    result = CliRunner().invoke(cli, ["contract", "check"])
    assert result.exit_code == 3
    assert "content hash does not match" in result.output

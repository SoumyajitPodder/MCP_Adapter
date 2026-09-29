from pathlib import Path

import pytest
from scripts import review_label
from scripts.review_label import Event, approver, parse_codeowners, parse_pages

pytestmark = pytest.mark.unit

LABEL = "contract-review-approved"
OWNERS = frozenset({"owner"})


def _event(kind: str, login: str, at: str, label: str = LABEL) -> Event:
    return {"event": kind, "actor": {"login": login}, "label": {"name": label}, "created_at": at}


def test_owner_applied_label_approves() -> None:
    events = [_event("labeled", "Owner", "2026-01-01T00:00:00Z")]
    assert approver(events, LABEL, OWNERS) == "Owner"


def test_non_owner_applied_label_does_not_approve() -> None:
    events = [_event("labeled", "triager", "2026-01-01T00:00:00Z")]
    assert approver(events, LABEL, OWNERS) is None


def test_label_removed_after_owner_applied_does_not_approve() -> None:
    events = [
        _event("labeled", "owner", "2026-01-01T00:00:00Z"),
        _event("unlabeled", "triager", "2026-01-02T00:00:00Z"),
    ]
    assert approver(events, LABEL, OWNERS) is None


def test_reapplied_by_non_owner_does_not_approve() -> None:
    events = [
        _event("labeled", "owner", "2026-01-01T00:00:00Z"),
        _event("unlabeled", "triager", "2026-01-02T00:00:00Z"),
        _event("labeled", "triager", "2026-01-03T00:00:00Z"),
    ]
    assert approver(events, LABEL, OWNERS) is None


def test_other_labels_and_event_order_are_handled() -> None:
    events = [
        _event("labeled", "owner", "2026-01-02T00:00:00Z"),
        _event("labeled", "triager", "2026-01-01T00:00:00Z"),
        _event("unlabeled", "triager", "2026-01-03T00:00:00Z", label="other"),
        {"event": "commented", "actor": {"login": "triager"}, "created_at": "2026-01-04"},
    ]
    assert approver(events, LABEL, OWNERS) == "owner"


def test_team_only_owners_never_approve() -> None:
    owners = parse_codeowners("/contracts/ @org/platform\n")
    assert owners == frozenset()
    events = [_event("labeled", "platform-member", "2026-01-01T00:00:00Z")]
    assert approver(events, LABEL, owners) is None


def test_codeowners_parsing_ignores_comments_and_blanks() -> None:
    text = (
        "# owners for gates @ignored\n"
        "\n"
        "/contracts/   @Alice @org/team\n"
        "/policies/ @bob # trailing @notme\n"
        "/docs/ docs@example.com\n"
    )
    assert parse_codeowners(text) == frozenset({"alice", "bob"})


def test_paginated_output_is_concatenated() -> None:
    assert parse_pages('[{"id": 1}]\n[{"id": 2}, 3]\n') == [{"id": 1}, {"id": 2}]
    assert parse_pages("") == []
    with pytest.raises(TypeError, match="unexpected"):
        parse_pages('{"message": "Not Found"}')


def _run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, events: list[Event]) -> tuple[int, str]:
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("/contracts/ @owner\n", encoding="utf-8")
    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(review_label, "fetch_events", lambda _repo, _pr: events)
    argv = ["--pr", "7", "--repo", "o/r", "--label", LABEL, "--codeowners", str(codeowners)]
    code = review_label.main(argv)
    return code, output.read_text(encoding="utf-8") if output.exists() else ""


def test_main_writes_outputs_when_approved(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(tmp_path, monkeypatch, [_event("labeled", "owner", "2026-01-01")])
    assert code == 0
    assert out == "approved=true\napprover=owner\n"
    assert capsys.readouterr().out == "approved by owner\n"


def test_main_reports_non_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(tmp_path, monkeypatch, [_event("labeled", "triager", "2026-01-01")])
    assert code == 0
    assert out == "approved=false\napprover=\n"
    assert "triager" in capsys.readouterr().out


def test_main_reports_missing_label(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    code, out = _run(tmp_path, monkeypatch, [])
    assert code == 0
    assert out == "approved=false\napprover=\n"
    assert "not applied" in capsys.readouterr().out


def test_main_fails_closed_on_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(_repo: str, _pr: int) -> list[Event]:
        raise RuntimeError("gh api failed")

    output = tmp_path / "out"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    monkeypatch.setattr(review_label, "fetch_events", boom)
    codeowners = tmp_path / "CODEOWNERS"
    codeowners.write_text("* @owner\n", encoding="utf-8")
    argv = ["--pr", "7", "--repo", "o/r", "--label", LABEL, "--codeowners", str(codeowners)]
    assert review_label.main(argv) == 1
    assert not output.exists()

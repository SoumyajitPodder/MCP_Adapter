"""Decide whether a PR's review label is currently applied by a code owner (brief §5.7).

Usage (CI, with GH_TOKEN set):
    python scripts/review_label.py --pr 12 --repo org/name
        --label contract-review-approved --codeowners .github/CODEOWNERS

Prints one line and, when $GITHUB_OUTPUT is set, writes ``approved=true|false`` and
``approver=<login>``. Exits 0 either way; non-zero only on errors, which callers must treat
as not approved.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
from collections.abc import Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

type Event = Mapping[str, Any]


def parse_codeowners(text: str) -> frozenset[str]:
    """Individual ``@user`` owners, lowercased.

    Team entries (``@org/team``) are skipped: resolving team membership needs a token with
    ``read:org``, which the workflow token lacks, so a team never counts as an approver.
    Email owners are skipped too.
    """
    owners: set[str] = set()
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        for token in line.split()[1:]:
            if token.startswith("@") and "/" not in token and len(token) > 1:
                owners.add(token[1:].lower())
    return frozenset(owners)


def current_labeler(events: Iterable[Event], label: str) -> str | None:
    """Login that applied ``label`` if it is still on the PR, else None.

    The deciding event is the latest ``labeled`` one not followed by an ``unlabeled`` one.
    """
    wanted = label.casefold()
    actor: str | None = None
    for event in sorted(events, key=lambda e: str(e.get("created_at", ""))):
        name = str((event.get("label") or {}).get("name", "")).casefold()
        if name != wanted:
            continue
        if event.get("event") == "labeled":
            actor = str((event.get("actor") or {}).get("login", "")) or None
        elif event.get("event") == "unlabeled":
            actor = None
    return actor


def approver(events: Iterable[Event], label: str, owners: frozenset[str]) -> str | None:
    """The code owner whose label currently approves the PR, else None."""
    actor = current_labeler(events, label)
    return actor if actor is not None and actor.lower() in owners else None


def parse_pages(output: str) -> list[Event]:
    """``gh api --paginate`` prints one JSON array per page back to back."""
    decoder = json.JSONDecoder()
    events: list[Event] = []
    pos = 0
    while (pos := _skip_space(output, pos)) < len(output):
        page, pos = decoder.raw_decode(output, pos)
        if not isinstance(page, list):
            raise TypeError("unexpected issue events payload")
        events.extend(e for e in page if isinstance(e, dict))
    return events


def _skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def fetch_events(repo: str, pr: int) -> list[Event]:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI not found")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [gh, "api", "--paginate", f"repos/{repo}/issues/{pr}/events"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed: {proc.stderr.strip()}")
    return parse_pages(proc.stdout)


def _write_outputs(values: Mapping[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8", newline="\n") as fh:
            fh.writelines(f"{k}={v}\n" for k, v in values.items())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--label", required=True)
    parser.add_argument("--codeowners", type=Path, required=True)
    args = parser.parse_args(argv)

    try:
        owners = parse_codeowners(args.codeowners.read_text(encoding="utf-8"))
        events = fetch_events(args.repo, args.pr)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    login = approver(events, args.label, owners)
    if login is not None:
        print(f"approved by {login}")
    else:
        actor = current_labeler(events, args.label)
        if actor is None:
            print(f"not approved: label {args.label!r} is not applied")
        else:
            print(f"not approved: {args.label!r} applied by {actor}, not an individual code owner")
    _write_outputs({"approved": str(login is not None).lower(), "approver": login or ""})
    return 0


if __name__ == "__main__":
    sys.exit(main())

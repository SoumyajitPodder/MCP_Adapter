"""Decide whether a PR's review label is currently applied by a code owner (brief §5.7).

Usage (CI, with GH_TOKEN set):
    python scripts/review_label.py --pr 12 --repo org/name --head-sha <sha>
        --label contract-review-approved --codeowners-ref <base sha>

The label counts only if a code owner applied it after the PR head was pushed: an approval
is for the commits the owner saw, not for later ones. Owners come from CODEOWNERS on the base
commit (``--codeowners-ref``), so a PR can't make its author an owner; ``--codeowners`` reads a
local file instead, for local runs.

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
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

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


class Labeling(NamedTuple):
    actor: str
    at: str


def current_labeler(events: Iterable[Event], label: str) -> Labeling | None:
    """Who applied ``label`` and when, if it is still on the PR, else None.

    The deciding event is the latest ``labeled`` one not followed by an ``unlabeled`` one.
    """
    wanted = label.casefold()
    current: Labeling | None = None
    for event in sorted(events, key=lambda e: str(e.get("created_at", ""))):
        name = str((event.get("label") or {}).get("name", "")).casefold()
        if name != wanted:
            continue
        if event.get("event") == "labeled":
            actor = str((event.get("actor") or {}).get("login", ""))
            current = Labeling(actor, str(event.get("created_at", ""))) if actor else None
        elif event.get("event") == "unlabeled":
            current = None
    return current


def approver(
    events: Iterable[Event], label: str, owners: frozenset[str], *, head_pushed_at: datetime
) -> str | None:
    """The code owner whose label approves the PR's current head, else None."""
    current = current_labeler(events, label)
    if current is None or current.actor.lower() not in owners:
        return None
    return current.actor if timestamp(current.at) > head_pushed_at else None


def timestamp(text: str) -> datetime:
    """A GitHub API timestamp. Raises ValueError for anything without a timezone."""
    at = datetime.fromisoformat(text)
    if at.tzinfo is None:
        raise ValueError(f"timestamp without timezone: {text!r}")
    return at


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


def _gh_api(*args: str) -> str:
    gh = shutil.which("gh")
    if gh is None:
        raise RuntimeError("gh CLI not found")
    proc = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [gh, "api", *args],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"gh api failed: {proc.stderr.strip()}")
    return proc.stdout


def fetch_events(repo: str, pr: int) -> list[Event]:
    return parse_pages(_gh_api("--paginate", f"repos/{repo}/issues/{pr}/events"))


def fetch_codeowners(repo: str, ref: str) -> str:
    return _gh_api(
        "-H",
        "Accept: application/vnd.github.raw+json",
        f"repos/{repo}/contents/.github/CODEOWNERS?ref={ref}",
    )


def fetch_head_pushed_at(repo: str, sha: str) -> datetime:
    """When the head commit reached GitHub: its earliest check suite (server time).

    The commit's own dates are set by its author, so they can't be trusted here. A PR that goes
    back to a commit pushed earlier keeps that commit's first push time.
    """
    output = _gh_api("--paginate", f"repos/{repo}/commits/{sha}/check-suites")
    return parse_first_suite_time(output)


def parse_first_suite_time(output: str) -> datetime:
    """Earliest ``created_at`` over ``gh api --paginate`` pages of check suites."""
    decoder = json.JSONDecoder()
    times: list[datetime] = []
    pos = 0
    while (pos := _skip_space(output, pos)) < len(output):
        page, pos = decoder.raw_decode(output, pos)
        if not isinstance(page, dict) or not isinstance(page.get("check_suites"), list):
            raise TypeError("unexpected check suites payload")
        times.extend(timestamp(str(s["created_at"])) for s in page["check_suites"])
    if not times:
        raise ValueError("the head commit has no check suites")
    return min(times)


def _write_outputs(values: Mapping[str, str]) -> None:
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with Path(path).open("a", encoding="utf-8", newline="\n") as fh:
            fh.writelines(f"{k}={v}\n" for k, v in values.items())


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--pr", type=int, required=True)
    parser.add_argument("--repo", required=True, help="owner/name")
    parser.add_argument("--head-sha", required=True, help="the PR head commit being checked")
    parser.add_argument("--label", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--codeowners-ref", help="base commit to read .github/CODEOWNERS from")
    source.add_argument("--codeowners", type=Path, help="local CODEOWNERS file (local runs)")
    args = parser.parse_args(argv)

    try:
        if args.codeowners is not None:
            text = args.codeowners.read_text(encoding="utf-8")
        else:
            text = fetch_codeowners(args.repo, args.codeowners_ref)
        owners = parse_codeowners(text)
        events = fetch_events(args.repo, args.pr)
        pushed_at = fetch_head_pushed_at(args.repo, args.head_sha)
        login = approver(events, args.label, owners, head_pushed_at=pushed_at)
    except (OSError, RuntimeError, TypeError, ValueError, KeyError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    if login is not None:
        print(f"approved by {login}")
    else:
        current = current_labeler(events, args.label)
        if current is None:
            print(f"not approved: label {args.label!r} is not applied")
        elif current.actor.lower() not in owners:
            print(
                f"not approved: {args.label!r} applied by {current.actor},"
                " not an individual code owner"
            )
        else:
            print(f"not approved: {args.label!r} was applied before the latest push; re-apply it")
    _write_outputs({"approved": str(login is not None).lower(), "approver": login or ""})
    return 0


if __name__ == "__main__":
    sys.exit(main())

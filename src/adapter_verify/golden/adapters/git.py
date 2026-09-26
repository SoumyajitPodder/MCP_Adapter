"""Read-only git queries for change selection (§6.7) and the report's description diffs."""

import subprocess
from pathlib import Path


class GitError(Exception):
    """git failed or isn't available."""


class GitRepo:
    def __init__(self, root: Path) -> None:
        self._root = root

    def _git(self, *args: str) -> str:
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell
                ["git", *args],  # noqa: S607 - git from PATH, as in CI
                cwd=self._root,
                capture_output=True,
                text=True,
                encoding="utf-8",
                check=True,
                timeout=60,
            )
        except (OSError, subprocess.SubprocessError):
            msg = f"git {args[0]} failed"
            raise GitError(msg) from None
        return done.stdout

    def changed_paths(self, base: str) -> list[str]:
        """Files that differ from ``base`` in the working tree, plus untracked files."""
        tracked = self._git("diff", "--name-only", base, "--").splitlines()
        untracked = self._git("ls-files", "--others", "--exclude-standard").splitlines()
        return sorted({p for p in (*tracked, *untracked) if p})

    def show(self, ref: str, path: str) -> str | None:
        """A file's content at ``ref``, or None if it didn't exist there."""
        try:
            return self._git("show", f"{ref}:{path}")
        except GitError:
            return None

    def head(self) -> str | None:
        try:
            return self._git("rev-parse", "HEAD").strip() or None
        except GitError:
            return None

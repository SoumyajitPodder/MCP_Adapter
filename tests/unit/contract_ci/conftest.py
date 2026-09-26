"""Shared contract-CI fixtures."""

import shutil
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name in ("sources", "baselines", "contracts", "fixtures", "policies", "catalog"):
        shutil.copytree(REPO / name, tmp_path / name)
    shutil.copy(REPO / "credentials.yaml", tmp_path / "credentials.yaml")
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    return tmp_path

import os

import pytest
from hypothesis import settings

from adapter_verify.settings import GoldenSettings

pytest_plugins = ["tests.sentinels", "pytester"]

# "ci" is thorough and reproducible; "dev" keeps the local loop fast. CI sets HYPOTHESIS_PROFILE=ci.
settings.register_profile("ci", max_examples=500, derandomize=True, deadline=None)
settings.register_profile("dev", max_examples=50)
settings.load_profile(os.environ.get("HYPOTHESIS_PROFILE", "dev"))


@pytest.fixture(autouse=True)
def _no_real_model_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """Tests never see a real model key: not from the environment, not from a local .env."""
    monkeypatch.delenv("ADAPTER_GOLDEN_GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("ADAPTER_GOLDEN_NVIDIA_API_KEY", raising=False)
    monkeypatch.setitem(GoldenSettings.model_config, "env_file", None)

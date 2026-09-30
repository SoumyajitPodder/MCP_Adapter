from pathlib import Path

import pytest
from pydantic import ValidationError

from adapter_verify.access.adapters.files import (
    ConfigFileError,
    load_catalog,
    load_credential_map,
    load_policy_documents,
)
from adapter_verify.access.adapters.secrets import EnvSecretManager, env_var_for
from adapter_verify.access.domain.credentials import CredentialBinding, CredentialMap
from adapter_verify.access.domain.lint import lint
from adapter_verify.access.domain.policy import Scope
from adapter_verify.access.domain.semver import Version
from adapter_verify.access.ports import SecretUnavailableError

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[3]


def test_repository_config_is_valid_and_lint_clean() -> None:
    catalog = load_catalog(REPO / "catalog" / "tools.yaml")
    documents = load_policy_documents(REPO / "policies")
    assert documents
    assert lint(documents, catalog) == []
    names = load_credential_map(REPO / "credentials.yaml")
    assert names.secret_for("order.get", Version.parse("1.2.0")) == "order-management/read"


def test_policy_file_errors_become_documents(tmp_path: Path) -> None:
    (tmp_path / "ok.yaml").write_text(
        "agent_id: ok\nowner: t\nscope: read\ngrants: []\n", encoding="utf-8"
    )
    (tmp_path / "bad-yaml.yaml").write_text("agent_id: [", encoding="utf-8")
    (tmp_path / "bad-field.yaml").write_text(
        "agent_id: x\nowner: t\nscope: admin\ngrants: []\n", encoding="utf-8"
    )
    (tmp_path / "date.yaml").write_text(
        "agent_id: x\nowner: t\nscope: read\ngrants: []\nreviewed: 2026-01-01\n", encoding="utf-8"
    )
    (tmp_path / "latin1.yaml").write_bytes(b"agent_id: caf\xe9\n")
    (tmp_path / "dir.yaml").mkdir()  # reading it raises IsADirectoryError
    docs = {d.file_name: d for d in load_policy_documents(tmp_path)}
    assert docs["ok.yaml"].policy is not None
    assert docs["ok.yaml"].policy.scope is Scope.READ
    assert docs["bad-yaml.yaml"].load_error == "not valid YAML"
    assert docs["bad-field.yaml"].load_error == "invalid fields: scope"
    assert "not plain JSON" in (docs["date.yaml"].load_error or "")
    assert docs["latin1.yaml"].load_error == "unreadable: UnicodeDecodeError"
    assert docs["dir.yaml"].load_error == "unreadable: IsADirectoryError"


def test_catalog_errors_raise(tmp_path: Path) -> None:
    path = tmp_path / "tools.yaml"
    path.write_text(
        "tools:\n  - tool: a.b\n    version: 1\n    behavior: read_only\n", encoding="utf-8"
    )
    with pytest.raises(ConfigFileError, match=r"tools\.0\.version"):
        load_catalog(path)


def test_credential_map_rejects_two_bindings_for_one_tool() -> None:
    binding = CredentialBinding(tool="a.b", versions=">=1.0.0,<2.0.0", secret_name="x")
    with pytest.raises(ValidationError, match="one credential binding per tool"):
        CredentialMap(bindings=(binding, binding))
    assert CredentialMap(bindings=(binding,)).secret_for("a.b", Version.parse("3.0.0")) is None


@pytest.mark.asyncio
async def test_env_secret_manager(monkeypatch: pytest.MonkeyPatch) -> None:
    assert env_var_for("order-management/read") == "ADAPTER_SECRET_ORDER_MANAGEMENT_READ"
    monkeypatch.setenv("ADAPTER_SECRET_ORDER_MANAGEMENT_READ", "value-from-env")
    secret = await EnvSecretManager().get("order-management/read")
    assert secret.get_secret_value() == "value-from-env"
    assert "value-from-env" not in repr(secret)
    with pytest.raises(SecretUnavailableError):
        await EnvSecretManager().get("missing")

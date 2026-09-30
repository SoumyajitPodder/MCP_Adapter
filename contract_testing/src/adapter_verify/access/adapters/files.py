"""Load policies, the tool catalog and credential names from reviewed YAML files."""

from pathlib import Path

from adapter_verify.access.domain.credentials import CredentialMap
from adapter_verify.access.domain.lint import PolicyDocument
from adapter_verify.access.domain.policy import Policy, ToolCatalog
from adapter_verify.common.adapters.yamlfile import ConfigFileError, parse_yaml_model

__all__ = ["ConfigFileError", "load_catalog", "load_credential_map", "load_policy_documents"]


def load_policy_documents(directory: Path) -> list[PolicyDocument]:
    documents: list[PolicyDocument] = []
    for path in sorted(directory.glob("*.yaml")):
        try:
            policy = parse_yaml_model(Policy, path.read_text(encoding="utf-8"))
            documents.append(PolicyDocument(file_name=path.name, policy=policy, load_error=None))
        except ConfigFileError as exc:
            documents.append(PolicyDocument(file_name=path.name, policy=None, load_error=str(exc)))
        except (OSError, UnicodeDecodeError) as exc:
            # Unreadable is a load error like unparseable: startup fails, a reload keeps the
            # last good set.
            error = f"unreadable: {type(exc).__name__}"
            documents.append(PolicyDocument(file_name=path.name, policy=None, load_error=error))
    return documents


def load_catalog(path: Path) -> ToolCatalog:
    return parse_yaml_model(ToolCatalog, path.read_text(encoding="utf-8"))


def load_credential_map(path: Path) -> CredentialMap:
    return parse_yaml_model(CredentialMap, path.read_text(encoding="utf-8"))

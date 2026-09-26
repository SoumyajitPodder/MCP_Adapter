"""File-backed contract repository: everything contract CI reads lives in reviewed files."""

import json
from collections.abc import Sequence
from datetime import datetime
from importlib import resources
from pathlib import Path

from pydantic import ValidationError

from adapter_kernel.shape import Shape
from adapter_verify.common.adapters.yamlfile import ConfigFileError, parse_yaml_model
from adapter_verify.contract_ci.domain.contracts import CanonicalContract, ReleaseLock
from adapter_verify.contract_ci.domain.extract import Sample
from adapter_verify.contract_ci.domain.rules import RuleBook
from adapter_verify.contract_ci.domain.sources import SourceConfig


def read_shape(path: Path) -> Shape:
    """Read a baseline, verifying its recorded content hash (a hand-edited baseline fails)."""
    body = json.loads(path.read_text(encoding="utf-8"))
    recorded = body.pop("content_hash", None)
    shape = Shape.model_validate_json(json.dumps(body))
    if recorded is not None and recorded != shape.content_hash():
        msg = (
            f"{path.name}: content hash does not match; baselines change only via `baseline accept`"
        )
        raise ConfigFileError(msg)
    return shape


def load_rulebook() -> RuleBook:
    text = (
        resources.files("adapter_verify.contract_ci")
        .joinpath("rules.yaml")
        .read_text(encoding="utf-8")
    )
    return parse_yaml_model(RuleBook, text)


class FileContractRepository:
    def __init__(
        self,
        *,
        root: Path,
        sources_dir: Path,
        baselines_dir: Path,
        contracts_dir: Path,
        mappings_dir: Path,
    ) -> None:
        self._root = root
        self._sources_dir = sources_dir
        self._baselines_dir = baselines_dir
        self._contracts_dir = contracts_dir
        self._mappings_dir = mappings_dir

    def rulebook(self) -> RuleBook:
        return load_rulebook()

    def sources(self) -> list[SourceConfig]:
        found: list[SourceConfig] = []
        for path in sorted(self._sources_dir.glob("*.yaml")):
            try:
                found.append(parse_yaml_model(SourceConfig, path.read_text(encoding="utf-8")))
            except ConfigFileError as exc:
                msg = f"{path.name}: {exc}"
                raise ConfigFileError(msg) from None
        return found

    def json_samples(self, source: SourceConfig) -> list[Sample]:
        samples: list[Sample] = []
        for sample_file in source.samples:
            for n, line in enumerate(self._read(sample_file.path).splitlines(), 1):
                if not line.strip():
                    continue
                try:
                    samples.append(Sample.model_validate_json(line))
                except ValidationError:
                    msg = f"{sample_file.path}:{n} is not a sample record"
                    raise ValueError(msg) from None
        return samples

    def csv_files(self, source: SourceConfig) -> list[tuple[datetime, str]]:
        return [
            (s.captured_at, self._read(s.path)) for s in source.samples if s.captured_at is not None
        ]

    def baseline(self, source: SourceConfig) -> Shape | None:
        directory = self._baselines_dir / source.source_id
        exact = directory / f"{source.version}.shape.json"
        if exact.exists():
            return read_shape(exact)
        shapes = [read_shape(p) for p in directory.glob("*.shape.json")]
        return max(shapes, key=lambda s: s.provenance.extracted_at, default=None)

    def write_baseline(self, shape: Shape) -> str:
        directory = self._baselines_dir / shape.source_id
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{shape.source_version}.shape.json"
        body = json.loads(shape.model_dump_json())
        body["content_hash"] = shape.content_hash()
        path.write_text(
            json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n"
        )
        return (
            path.relative_to(self._root).as_posix()
            if path.is_relative_to(self._root)
            else str(path)
        )

    def contracts(self) -> Sequence[CanonicalContract]:
        found: list[CanonicalContract] = []
        for path in sorted(self._contracts_dir.glob("*/*.yaml")):
            contract = parse_yaml_model(CanonicalContract, path.read_text(encoding="utf-8"))
            if (path.parent.name, path.stem) != (contract.tool, contract.version):
                msg = f"{path.as_posix()} must be named <tool>/<version>.yaml"
                raise ConfigFileError(msg)
            found.append(contract)
        return found

    def release_lock(self) -> ReleaseLock:
        path = self._contracts_dir / "released.json"
        return (
            ReleaseLock.model_validate_json(path.read_text(encoding="utf-8"))
            if path.exists()
            else ReleaseLock()
        )

    def write_release_lock(self, lock: ReleaseLock) -> None:
        path = self._contracts_dir / "released.json"
        body = json.dumps(lock.model_dump(), indent=2, sort_keys=True) + "\n"
        path.write_text(body, encoding="utf-8", newline="\n")

    def mappings_present(self) -> bool:
        return self._mappings_dir.exists() and any(self._mappings_dir.iterdir())

    def _read(self, relative: str) -> str:
        path = (self._root / relative).resolve()
        if not path.is_relative_to(self._root.resolve()):
            msg = f"sample path escapes the repository: {relative}"
            raise ValueError(msg)
        return path.read_text(encoding="utf-8")

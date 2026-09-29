"""Contract CI orchestration: run the four checks, gate, report; accept baselines; release
contracts. Reviewed files change only through these explicit operations (brief §5.9)."""

from dataclasses import dataclass

from adapter_kernel.shape import Shape, SourceKind
from adapter_verify.access.domain.credentials import CredentialMap
from adapter_verify.access.domain.policy import PolicySet, ToolCatalog
from adapter_verify.common.ports import Clock
from adapter_verify.contract_ci.domain.checks import (
    CheckResult,
    GateResult,
    canonical_check,
    gate,
    mapping_check,
    orphan_check,
    upstream_check,
)
from adapter_verify.contract_ci.domain.extract import (
    EvidenceThreshold,
    ExtractionError,
    extract_csv,
    extract_layout,
    extract_observed,
)
from adapter_verify.contract_ci.domain.report import render_markdown
from adapter_verify.contract_ci.domain.sources import SourceConfig
from adapter_verify.contract_ci.ports import ContractRepository


class ReleaseError(Exception):
    pass


@dataclass(frozen=True)
class AccessView:
    """What contract CI needs from access config: catalog, credential names, who calls what."""

    catalog: ToolCatalog
    credentials: CredentialMap
    policies: PolicySet


class ContractCi:
    def __init__(
        self,
        repo: ContractRepository,
        clock: Clock,
        *,
        threshold: EvidenceThreshold,
        rename_threshold: float,
    ) -> None:
        self._repo = repo
        self._clock = clock
        self._threshold = threshold
        self._rename_threshold = rename_threshold

    def sources(self) -> list[SourceConfig]:
        return self._repo.sources()

    def extract(self, source: SourceConfig) -> Shape:
        """Shape of the current samples. Raises ExtractionError or ValueError."""
        at = self._clock.now()
        if source.kind is SourceKind.FILE and source.layout is not None:
            return extract_csv(
                self._repo.csv_files(source),
                layout=source.layout,
                delimiter=source.delimiter,
                source_id=source.source_id,
                version=source.version,
                operation=source.operation,
                threshold=self._threshold,
                extracted_at=at,
            )
        return extract_observed(
            self._repo.json_samples(source),
            source_id=source.source_id,
            version=source.version,
            operation=source.operation,
            enum_paths=source.enum_paths,
            threshold=self._threshold,
            extracted_at=at,
        )

    def accept_baseline(self, source_id: str) -> str:
        """Write the source's new baseline for review. A declared layout wins over samples."""
        source = self._source(source_id)
        if source.kind is SourceKind.FILE and source.layout is not None:
            shape = extract_layout(
                source.layout,
                source_id=source.source_id,
                version=source.version,
                operation=source.operation,
                extracted_at=self._clock.now(),
            )
        else:
            shape = self.extract(source)
        return self._repo.write_baseline(shape)

    def release(self, tool: str, version: str) -> None:
        key = f"{tool}@{version}"
        contract = next((c for c in self._repo.contracts() if c.key == key), None)
        if contract is None:
            msg = f"no contract file for {key}"
            raise ReleaseError(msg)
        lock = self._repo.release_lock()
        digest = contract.shape(self._clock.now()).content_hash()
        if lock.released.get(key, digest) != digest:
            msg = f"{key} is already released with different content"
            raise ReleaseError(msg)
        self._repo.write_release_lock(
            lock.model_copy(update={"released": {**lock.released, key: digest}})
        )

    def check(self, access: AccessView, *, review_approved: bool) -> tuple[GateResult, str]:
        book = self._repo.rulebook()
        sources = self._repo.sources()
        contracts = self._repo.contracts()
        results: list[CheckResult] = []
        for source in sources:
            current, error = None, None
            try:
                current = self.extract(source)
            except (ExtractionError, ValueError) as exc:
                error = str(exc)
            results.append(
                upstream_check(
                    source.source_id,
                    source.tools,
                    self._repo.baseline(source),
                    current,
                    book,
                    rename_threshold=self._rename_threshold,
                    extraction_error=error,
                )
            )
        results += canonical_check(contracts, self._repo.release_lock(), book, at=self._clock.now())
        results.append(mapping_check(mappings_present=self._repo.mappings_present()))
        results.append(
            orphan_check(
                access.catalog.names(),
                {c.tool for c in contracts},
                {b.tool for b in access.credentials.bindings},
                {s.source_id: s.tools for s in sources},
            )
        )
        result = gate(results, review_approved=review_approved)
        return result, render_markdown(result, access.policies.callers())

    def _source(self, source_id: str) -> SourceConfig:
        for source in self._repo.sources():
            if source.source_id == source_id:
                return source
        msg = f"unknown source {source_id}"
        raise KeyError(msg)

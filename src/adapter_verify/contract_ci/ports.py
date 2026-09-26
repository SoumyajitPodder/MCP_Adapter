"""Where contract CI reads its inputs and writes reviewed outputs."""

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol

from adapter_kernel.shape import Shape
from adapter_verify.contract_ci.domain.contracts import CanonicalContract, ReleaseLock
from adapter_verify.contract_ci.domain.extract import Sample
from adapter_verify.contract_ci.domain.rules import RuleBook
from adapter_verify.contract_ci.domain.sources import SourceConfig


class ContractRepository(Protocol):
    def rulebook(self) -> RuleBook: ...

    def sources(self) -> list[SourceConfig]: ...

    def json_samples(self, source: SourceConfig) -> list[Sample]:
        """JSONL samples of an observed source. Raises ValueError if unreadable."""
        ...

    def csv_files(self, source: SourceConfig) -> list[tuple[datetime, str]]:
        """(captured_at, text) of a file source's sample files."""
        ...

    def baseline(self, source: SourceConfig) -> Shape | None:
        """The accepted baseline: this version's, else the most recently extracted one."""
        ...

    def write_baseline(self, shape: Shape) -> str:
        """Write a baseline for PR review. Returns its repository path."""
        ...

    def contracts(self) -> Sequence[CanonicalContract]: ...

    def release_lock(self) -> ReleaseLock: ...

    def write_release_lock(self, lock: ReleaseLock) -> None: ...

    def mappings_present(self) -> bool: ...

"""Tamper-evident audit records: hash chains and their verification (brief §8.7, R-011).

Each record's hash covers its chain, position, predecessor hash and event, so editing,
reordering or deleting a record breaks verification of everything after it. Chains are
partitioned (``chain_id_for``) to avoid a global lock; an anchor chain periodically records
every chain's head so whole-chain deletion and truncation behind an anchor are detected too.

Known limit: records appended after the latest anchor can be truncated from the chain tail
undetected; the anchor interval bounds that window.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping, Sequence
from enum import StrEnum
from typing import Final

from pydantic import AwareDatetime, Field

from adapter_kernel.shape import canonical_json
from adapter_verify.common.model import FrozenModel

ANCHOR_CHAIN: Final = "anchor:global"
_PRINCIPAL = re.compile(r"[A-Za-z0-9._:-]{1,128}")

type DetailValue = str | int | bool | None


class AuditKind(StrEnum):
    ACCESS_DECISION = "access_decision"
    IDEMPOTENCY_TRANSITION = "idempotency_transition"
    MANUAL_RECONCILIATION = "manual_reconciliation"
    BASELINE_ACCEPT = "baseline_accept"
    CI_OVERRIDE = "ci_override"
    ANCHOR = "anchor"


class PrincipalKind(StrEnum):
    AGENT = "agent"
    OPERATOR = "operator"
    SYSTEM = "system"


class AuditEvent(FrozenModel):
    """What happened. Details are flat scalars and must already be redacted."""

    kind: AuditKind
    actor: str = Field(min_length=1, description="Principal that caused the event.")
    occurred_at: AwareDatetime
    correlation_id: str | None = None
    detail: dict[str, DetailValue] = Field(default_factory=dict)


class AuditRecord(FrozenModel):
    chain_id: str = Field(min_length=1)
    seq: int = Field(ge=1)
    prev_hash: str | None = Field(pattern=r"^[0-9a-f]{64}$")
    record_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    event: AuditEvent


class ChainHead(FrozenModel):
    chain_id: str
    seq: int = Field(ge=1)
    record_hash: str


class ProblemKind(StrEnum):
    WRONG_CHAIN = "wrong_chain"
    SEQUENCE_GAP = "sequence_gap"
    PREVIOUS_HASH_MISMATCH = "previous_hash_mismatch"
    HASH_MISMATCH = "hash_mismatch"
    TRUNCATED_BEHIND_ANCHOR = "truncated_behind_anchor"
    ANCHOR_MISMATCH = "anchor_mismatch"
    MALFORMED_ANCHOR = "malformed_anchor"


class ChainProblem(FrozenModel):
    kind: ProblemKind
    chain_id: str
    seq: int


def chain_id_for(kind: PrincipalKind, principal: str) -> str:
    """One chain per principal. PENDING owner answer to M1-Q6; change only here."""
    if _PRINCIPAL.fullmatch(principal) is None:
        msg = "principal must match [A-Za-z0-9._:-]{1,128}"
        raise ValueError(msg)
    return f"{kind.value}:{principal}"


def record_hash(chain_id: str, seq: int, prev_hash: str | None, event: AuditEvent) -> str:
    body = {
        "chain_id": chain_id,
        "seq": seq,
        "prev_hash": prev_hash,
        "event": event.model_dump(mode="json"),
    }
    return hashlib.sha256(canonical_json(body)).hexdigest()


def link(chain_id: str, head: ChainHead | None, event: AuditEvent) -> AuditRecord:
    """The record that appends ``event`` after ``head`` (None for an empty chain)."""
    seq = 1 if head is None else head.seq + 1
    prev = None if head is None else head.record_hash
    return AuditRecord(
        chain_id=chain_id,
        seq=seq,
        prev_hash=prev,
        record_hash=record_hash(chain_id, seq, prev, event),
        event=event,
    )


def verify_chain(chain_id: str, records: Sequence[AuditRecord]) -> list[ChainProblem]:
    """Check one chain, given in ascending ``seq`` order."""
    problems: list[ChainProblem] = []
    prev: AuditRecord | None = None
    for record in records:
        if record.chain_id != chain_id:
            problems.append(_problem(ProblemKind.WRONG_CHAIN, chain_id, record.seq))
        expected_seq = 1 if prev is None else prev.seq + 1
        if record.seq != expected_seq:
            problems.append(_problem(ProblemKind.SEQUENCE_GAP, chain_id, record.seq))
        expected_prev = None if prev is None else prev.record_hash
        if record.prev_hash != expected_prev:
            problems.append(_problem(ProblemKind.PREVIOUS_HASH_MISMATCH, chain_id, record.seq))
        recomputed = record_hash(record.chain_id, record.seq, record.prev_hash, record.event)
        if recomputed != record.record_hash:
            problems.append(_problem(ProblemKind.HASH_MISMATCH, chain_id, record.seq))
        prev = record
    return problems


def anchor_event(head: ChainHead, actor: str, at: AwareDatetime) -> AuditEvent:
    return AuditEvent(
        kind=AuditKind.ANCHOR,
        actor=actor,
        occurred_at=at,
        detail={"chain_id": head.chain_id, "seq": head.seq, "record_hash": head.record_hash},
    )


def anchored_heads(anchors: Iterable[AuditRecord]) -> tuple[dict[str, ChainHead], list[int]]:
    """Latest anchored head per chain, plus the seqs of malformed anchor records."""
    latest: dict[str, ChainHead] = {}
    malformed: list[int] = []
    for record in anchors:
        detail = record.event.detail
        chain, seq, digest = detail.get("chain_id"), detail.get("seq"), detail.get("record_hash")
        if (
            record.event.kind is not AuditKind.ANCHOR
            or not isinstance(chain, str)
            or not isinstance(seq, int)
            or isinstance(seq, bool)
            or not isinstance(digest, str)
        ):
            malformed.append(record.seq)
            continue
        current = latest.get(chain)
        if current is None or seq >= current.seq:
            latest[chain] = ChainHead(chain_id=chain, seq=seq, record_hash=digest)
    return latest, malformed


def heads_to_anchor(
    heads: Iterable[ChainHead], anchored: Mapping[str, ChainHead]
) -> list[ChainHead]:
    """Chains whose head moved since they were last anchored (the anchor chain excluded)."""
    return [
        head
        for head in sorted(heads, key=lambda h: h.chain_id)
        if head.chain_id != ANCHOR_CHAIN and anchored.get(head.chain_id) != head
    ]


def verify_against_anchors(
    chains: Mapping[str, Sequence[AuditRecord]], anchors: Sequence[AuditRecord]
) -> list[ChainProblem]:
    """Every anchored head must still exist, unchanged, in its chain."""
    problems = verify_chain(ANCHOR_CHAIN, anchors)
    latest, malformed = anchored_heads(anchors)
    problems.extend(_problem(ProblemKind.MALFORMED_ANCHOR, ANCHOR_CHAIN, s) for s in malformed)
    for chain_id, head in sorted(latest.items()):
        by_seq = {r.seq: r for r in chains.get(chain_id, ())}
        found = by_seq.get(head.seq)
        if found is None:
            problems.append(_problem(ProblemKind.TRUNCATED_BEHIND_ANCHOR, chain_id, head.seq))
        elif found.record_hash != head.record_hash:
            problems.append(_problem(ProblemKind.ANCHOR_MISMATCH, chain_id, head.seq))
    return problems


def _problem(kind: ProblemKind, chain_id: str, seq: int) -> ChainProblem:
    return ChainProblem(kind=kind, chain_id=chain_id, seq=seq)

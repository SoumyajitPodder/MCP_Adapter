"""Audit trail orchestration: record, anchor, verify (brief §8.7)."""

from adapter_verify.common.ports import Clock
from adapter_verify.observability.domain.audit import (
    ANCHOR_CHAIN,
    AuditEvent,
    AuditKind,
    AuditRecord,
    ChainProblem,
    DetailValue,
    PrincipalKind,
    anchor_event,
    anchored_heads,
    chain_id_for,
    heads_to_anchor,
    verify_against_anchors,
    verify_chain,
)
from adapter_verify.observability.ports import AuditStore

ANCHOR_ACTOR = "system:audit-anchor"


class AuditTrail:
    """Callers decide what ``AuditUnavailableError`` means; mutating calls fail closed (M1-Q2)."""

    def __init__(self, store: AuditStore, clock: Clock) -> None:
        self._store = store
        self._clock = clock

    async def record(
        self,
        *,
        principal_kind: PrincipalKind,
        principal: str,
        kind: AuditKind,
        correlation_id: str | None,
        detail: dict[str, DetailValue],
    ) -> AuditRecord:
        if kind is AuditKind.ANCHOR:
            msg = "anchor records are written only by anchor()"
            raise ValueError(msg)
        event = AuditEvent(
            kind=kind,
            actor=f"{principal_kind.value}:{principal}",
            occurred_at=self._clock.now(),
            correlation_id=correlation_id,
            detail=detail,
        )
        return await self._store.append(chain_id_for(principal_kind, principal), event)

    async def anchor(self) -> int:
        """Record the head of every chain that moved since its last anchor."""
        latest, _ = anchored_heads(await self._store.read_chain(ANCHOR_CHAIN))
        pending = heads_to_anchor(await self._store.heads(), latest)
        for head in pending:
            await self._store.append(
                ANCHOR_CHAIN, anchor_event(head, ANCHOR_ACTOR, self._clock.now())
            )
        return len(pending)

    async def verify(self) -> list[ChainProblem]:
        chains = {
            head.chain_id: await self._store.read_chain(head.chain_id)
            for head in await self._store.heads()
        }
        anchors = chains.pop(ANCHOR_CHAIN, [])
        problems = [p for cid, rs in sorted(chains.items()) for p in verify_chain(cid, rs)]
        problems.extend(verify_against_anchors(chains, anchors))
        return problems

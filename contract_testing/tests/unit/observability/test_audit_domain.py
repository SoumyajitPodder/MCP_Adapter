from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from adapter_verify.observability.domain.audit import (
    ANCHOR_CHAIN,
    AuditEvent,
    AuditKind,
    AuditRecord,
    ChainHead,
    ChainProblem,
    PrincipalKind,
    ProblemKind,
    anchor_event,
    anchored_heads,
    chain_id_for,
    heads_to_anchor,
    link,
    verify_against_anchors,
    verify_chain,
)

pytestmark = pytest.mark.unit

_AT = datetime(2026, 5, 1, tzinfo=UTC)


def _event(n: int) -> AuditEvent:
    return AuditEvent(
        kind=AuditKind.ACCESS_DECISION,
        actor="agent:synthetic",
        occurred_at=_AT,
        correlation_id=f"c-{n}",
        detail={"rule_id": f"r{n}", "allowed": n % 2 == 0, "n": n},
    )


def _chain(length: int, chain_id: str = "agent:synthetic") -> list[AuditRecord]:
    records: list[AuditRecord] = []
    for n in range(length):
        head = (
            None
            if not records
            else ChainHead(
                chain_id=chain_id, seq=records[-1].seq, record_hash=records[-1].record_hash
            )
        )
        records.append(link(chain_id, head, _event(n)))
    return records


def _head(r: AuditRecord) -> ChainHead:
    return ChainHead(chain_id=r.chain_id, seq=r.seq, record_hash=r.record_hash)


def _kinds(problems: list[ChainProblem]) -> set[ProblemKind]:
    return {p.kind for p in problems}


def test_chain_ids_are_per_principal_and_validated() -> None:
    assert chain_id_for(PrincipalKind.AGENT, "order-status-agent") == "agent:order-status-agent"
    with pytest.raises(ValueError, match="principal"):
        chain_id_for(PrincipalKind.OPERATOR, "has space")


def test_intact_chain_verifies() -> None:
    records = _chain(5)
    assert [r.seq for r in records] == [1, 2, 3, 4, 5]
    assert records[0].prev_hash is None
    assert verify_chain("agent:synthetic", records) == []


def test_edited_event_is_detected() -> None:
    records = _chain(4)
    forged = records[2].event.model_copy(update={"detail": {"rule_id": "r-forged"}})
    records[2] = records[2].model_copy(update={"event": forged})
    assert _kinds(verify_chain("agent:synthetic", records)) == {ProblemKind.HASH_MISMATCH}


def test_deleted_middle_record_is_detected() -> None:
    records = _chain(4)
    del records[1]
    assert {ProblemKind.SEQUENCE_GAP, ProblemKind.PREVIOUS_HASH_MISMATCH} <= _kinds(
        verify_chain("agent:synthetic", records)
    )


def test_rehashed_forgery_still_breaks_the_next_link() -> None:
    records = _chain(4)
    forged_event = records[1].event.model_copy(update={"detail": {"rule_id": "forged"}})
    forged = link("agent:synthetic", _head(records[0]), forged_event)
    records[1] = forged
    assert _kinds(verify_chain("agent:synthetic", records)) == {ProblemKind.PREVIOUS_HASH_MISMATCH}


def test_record_moved_between_chains_is_detected() -> None:
    records = _chain(2, "agent:a")
    assert ProblemKind.WRONG_CHAIN in _kinds(verify_chain("agent:b", records))


@given(length=st.integers(min_value=1, max_value=8), data=st.data())
def test_any_single_field_edit_is_detected(length: int, data: st.DataObject) -> None:
    records = _chain(length)
    i = data.draw(st.integers(min_value=0, max_value=length - 1))
    field = data.draw(st.sampled_from(["seq", "prev_hash", "record_hash", "actor", "detail"]))
    original = records[i]
    if field == "seq":
        tampered = original.model_copy(update={"seq": original.seq + 1})
    elif field == "prev_hash":
        tampered = original.model_copy(update={"prev_hash": "f" * 64})
    elif field == "record_hash":
        tampered = original.model_copy(update={"record_hash": "e" * 64})
    elif field == "actor":
        event = original.event.model_copy(update={"actor": "agent:someone-else"})
        tampered = original.model_copy(update={"event": event})
    else:
        event = original.event.model_copy(update={"detail": {"allowed": True}})
        tampered = original.model_copy(update={"event": event})
    records[i] = tampered
    assert verify_chain("agent:synthetic", records) != []


def _anchors(*heads: ChainHead) -> list[AuditRecord]:
    records: list[AuditRecord] = []
    for h in heads:
        prev = None if not records else _head(records[-1])
        records.append(link(ANCHOR_CHAIN, prev, anchor_event(h, "system:audit-anchor", _AT)))
    return records


def test_truncation_behind_an_anchor_is_detected() -> None:
    chain = _chain(5)
    anchors = _anchors(_head(chain[-1]))
    assert verify_against_anchors({"agent:synthetic": chain}, anchors) == []
    problems = verify_against_anchors({"agent:synthetic": chain[:3]}, anchors)
    assert _kinds(problems) == {ProblemKind.TRUNCATED_BEHIND_ANCHOR}


def test_deleting_a_whole_chain_is_detected() -> None:
    chain = _chain(2)
    problems = verify_against_anchors({}, _anchors(_head(chain[-1])))
    assert _kinds(problems) == {ProblemKind.TRUNCATED_BEHIND_ANCHOR}


def test_rewriting_an_anchored_record_is_detected() -> None:
    chain = _chain(3)
    anchors = _anchors(_head(chain[-1]))
    rebuilt = [*_chain(2), link("agent:synthetic", _head(_chain(2)[-1]), _event(99))]
    assert _kinds(verify_against_anchors({"agent:synthetic": rebuilt}, anchors)) == {
        ProblemKind.ANCHOR_MISMATCH
    }


def test_truncation_after_the_last_anchor_is_the_documented_blind_spot() -> None:
    chain = _chain(5)
    anchors = _anchors(_head(chain[2]))
    assert verify_against_anchors({"agent:synthetic": chain[:3]}, anchors) == []


def test_malformed_anchor_records_are_reported() -> None:
    bogus = AuditEvent(kind=AuditKind.ANCHOR, actor="x", occurred_at=_AT, detail={"seq": True})
    anchors = [link(ANCHOR_CHAIN, None, bogus)]
    assert _kinds(verify_against_anchors({}, anchors)) == {ProblemKind.MALFORMED_ANCHOR}
    not_anchor = [link(ANCHOR_CHAIN, None, _event(1))]
    assert anchored_heads(not_anchor) == ({}, [1])


def test_only_moved_heads_are_anchored() -> None:
    a = ChainHead(chain_id="agent:a", seq=3, record_hash="a" * 64)
    b = ChainHead(chain_id="agent:b", seq=1, record_hash="b" * 64)
    anchor_head = ChainHead(chain_id=ANCHOR_CHAIN, seq=9, record_hash="c" * 64)
    assert heads_to_anchor([b, a, anchor_head], {}) == [a, b]
    assert heads_to_anchor([a, b], {"agent:a": a}) == [b]


def test_latest_anchor_per_chain_wins() -> None:
    old = ChainHead(chain_id="agent:a", seq=1, record_hash="a" * 64)
    new = ChainHead(chain_id="agent:a", seq=4, record_hash="b" * 64)
    latest, malformed = anchored_heads(_anchors(old, new))
    assert latest == {"agent:a": new}
    assert malformed == []

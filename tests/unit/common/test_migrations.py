import pytest

from adapter_verify.common.domain.migrations import MigrationError, parse, plan

pytestmark = pytest.mark.unit

A = parse("0001_first.sql", "CREATE TABLE a (id int);")
B = parse("0002_second.sql", "CREATE TABLE b (id int);")
C = parse("0003_third.sql", "CREATE TABLE c (id int);")


def test_parse_reads_version_and_checksums_content() -> None:
    assert A.version == 1
    assert len(A.checksum) == 64
    assert parse("0001_first.sql", "different").checksum != A.checksum


@pytest.mark.parametrize("name", ["1_first.sql", "0001-first.sql", "0001_First.sql", "0001_x.txt"])
def test_parse_rejects_bad_names(name: str) -> None:
    with pytest.raises(MigrationError, match=r"0001_description\.sql"):
        parse(name, "")


def test_plan_orders_pending_migrations() -> None:
    assert plan([C, A, B], {}) == [A, B, C]
    assert plan([C, A, B], {1: A.checksum}) == [B, C]
    assert plan([A, B], {1: A.checksum, 2: B.checksum}) == []


def test_plan_refuses_an_edited_applied_migration() -> None:
    with pytest.raises(MigrationError, match="edited after it ran"):
        plan([A, B], {1: "0" * 64})


def test_plan_refuses_unknown_applied_migrations() -> None:
    with pytest.raises(MigrationError, match="not in the repository"):
        plan([A], {1: A.checksum, 2: B.checksum})


def test_plan_refuses_duplicate_versions() -> None:
    with pytest.raises(MigrationError, match="share a version"):
        plan([A, parse("0001_other.sql", "x")], {})


def test_plan_refuses_out_of_order_backfill() -> None:
    with pytest.raises(MigrationError, match="older than the newest applied"):
        plan([A, B, C], {1: A.checksum, 3: C.checksum})

"""Forward-only, checksummed SQL migrations: the planning half (DESIGN.md Session 3, §9).

Files are named ``NNNN_description.sql``. Once applied, a file may never change: the runner
refuses to continue if an applied file's checksum differs, or if the database has a
migration the repository does not.
"""

import hashlib
import re
from collections.abc import Iterable, Mapping

from pydantic import Field

from adapter_verify.common.model import FrozenModel

_NAME = re.compile(r"(?P<version>\d{4})_[a-z0-9_]+\.sql")


class Migration(FrozenModel):
    version: int = Field(ge=1)
    name: str
    sql: str
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")


class MigrationError(Exception):
    """The migration set is inconsistent; applying anything would be unsafe."""


def parse(name: str, sql: str) -> Migration:
    match = _NAME.fullmatch(name)
    if match is None:
        msg = f"migration file name must look like 0001_description.sql: {name}"
        raise MigrationError(msg)
    return Migration(
        version=int(match["version"]),
        name=name,
        sql=sql,
        checksum=hashlib.sha256(sql.encode()).hexdigest(),
    )


def plan(available: Iterable[Migration], applied: Mapping[int, str]) -> list[Migration]:
    """Migrations still to apply, in order. ``applied`` maps version to recorded checksum."""
    ordered = sorted(available, key=lambda m: m.version)
    versions = [m.version for m in ordered]
    if len(set(versions)) != len(versions):
        msg = "two migration files share a version number"
        raise MigrationError(msg)
    by_version = {m.version: m for m in ordered}
    for version, checksum in applied.items():
        known = by_version.get(version)
        if known is None:
            msg = f"database has migration {version:04d}, which is not in the repository"
            raise MigrationError(msg)
        if known.checksum != checksum:
            msg = f"applied migration {known.name} was edited after it ran"
            raise MigrationError(msg)
    pending = [m for m in ordered if m.version not in applied]
    if applied and pending and pending[0].version < max(applied):
        msg = f"migration {pending[0].name} is older than the newest applied one"
        raise MigrationError(msg)
    return pending

"""SemVer 2.0 versions and version ranges for access grants (brief §9.2).

Ranges are comma-joined comparators, e.g. ``>=1.0.0,<2.0.0``. Pre-release versions never
satisfy a range (D-037, M2-Q4), and range bounds may not be pre-releases.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from functools import total_ordering
from typing import Self

_VERSION = re.compile(
    r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
)
_COMPARATOR = re.compile(r"\s*(>=|<=|==|>|<)\s*(\S+)\s*")


@total_ordering
@dataclass(frozen=True, slots=True)
class Version:
    major: int
    minor: int
    patch: int
    prerelease: tuple[str, ...] = ()

    @classmethod
    def parse(cls, text: str) -> Self:
        match = _VERSION.fullmatch(text)
        if match is None:
            msg = f"not a SemVer version: {text!r}"
            raise ValueError(msg)
        pre = tuple(match[4].split(".")) if match[4] else ()
        return cls(int(match[1]), int(match[2]), int(match[3]), pre)

    @property
    def is_prerelease(self) -> bool:
        return bool(self.prerelease)

    def __str__(self) -> str:
        core = f"{self.major}.{self.minor}.{self.patch}"
        return f"{core}-{'.'.join(self.prerelease)}" if self.prerelease else core

    def __lt__(self, other: object) -> bool:
        if not isinstance(other, Version):
            return NotImplemented
        return _sort_key(self) < _sort_key(other)


def _identifier_key(identifier: str) -> tuple[int, int, str]:
    # SemVer §11: numeric identifiers sort numerically and before alphanumeric ones.
    return (0, int(identifier), "") if identifier.isdigit() else (1, 0, identifier)


def _sort_key(v: Version) -> tuple[int, int, int, int, tuple[tuple[int, int, str], ...]]:
    # A release sorts after all of its pre-releases.
    return (
        v.major,
        v.minor,
        v.patch,
        0 if v.prerelease else 1,
        tuple(map(_identifier_key, v.prerelease)),
    )


class Op(StrEnum):
    GE = ">="
    GT = ">"
    LE = "<="
    LT = "<"
    EQ = "=="


@dataclass(frozen=True, slots=True)
class Comparator:
    op: Op
    version: Version

    def accepts(self, v: Version) -> bool:
        match self.op:
            case Op.GE:
                return v >= self.version
            case Op.GT:
                return v > self.version
            case Op.LE:
                return v <= self.version
            case Op.LT:
                return v < self.version
            case Op.EQ:
                return v == self.version


@dataclass(frozen=True, slots=True)
class VersionRange:
    comparators: tuple[Comparator, ...]

    @classmethod
    def parse(cls, text: str) -> Self:
        parts = text.split(",")
        comparators: list[Comparator] = []
        for part in parts:
            match = _COMPARATOR.fullmatch(part)
            if match is None:
                msg = f"not a version comparator: {part.strip()!r}"
                raise ValueError(msg)
            version = Version.parse(match[2])
            if version.is_prerelease:
                msg = "range bounds may not be pre-release versions"
                raise ValueError(msg)
            comparators.append(Comparator(Op(match[1]), version))
        return cls(tuple(comparators))

    def contains(self, v: Version) -> bool:
        return not v.is_prerelease and all(c.accepts(v) for c in self.comparators)

    def single_major(self) -> int | None:
        """The MAJOR this range is confined to, or None if it could span majors (§9.2)."""
        lowers = [c for c in self.comparators if c.op in (Op.GE, Op.GT, Op.EQ)]
        uppers = [c for c in self.comparators if c.op in (Op.LT, Op.LE, Op.EQ)]
        if not lowers or not uppers:
            return None
        major = max(c.version for c in lowers).major
        ceiling = Version(major + 1, 0, 0)
        confined = all(
            c.version <= ceiling if c.op is Op.LT else c.version.major == major for c in uppers
        )
        return major if confined else None

    def __str__(self) -> str:
        return ",".join(f"{c.op.value}{c.version}" for c in self.comparators)

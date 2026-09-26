"""Shape differ (brief §5.4-5.5): every structural difference becomes exactly one rule ID.

Property (tested): ``diff(a, b)`` is empty if and only if the content hashes match. A
difference no specific rule describes still surfaces as ``UNCLASSIFIED_CHANGE``.
"""

import difflib
import re
from collections.abc import Mapping, Sequence

from pydantic import Field

from adapter_kernel.classification import Classification
from adapter_kernel.shape import FieldShape, Shape
from adapter_verify.common.model import FrozenModel
from adapter_verify.contract_ci.domain.rules import CheckKind, RuleBook

type Aliases = Mapping[str, Mapping[str, str]]
"""operation → {old path: new path}, from the mapping registry (brief §5.5, FIELD_RENAMED_KNOWN)."""


class Change(FrozenModel):
    rule_id: str
    operation: str
    path: str
    detail: str


class Finding(FrozenModel):
    rule_id: str
    operation: str
    path: str
    detail: str
    classification: Classification
    note: str | None = Field(default=None, description="Why the rule's default was adjusted.")


def diff(
    base: Shape, rev: Shape, *, aliases: Aliases | None = None, rename_threshold: float = 0.6
) -> list[Change]:
    if base.source_id != rev.source_id:
        msg = "can only diff two shapes of the same source"
        raise ValueError(msg)
    aliases = aliases or {}
    b_ops = {o.name: o for o in base.operations}
    r_ops = {o.name: o for o in rev.operations}
    changes = [
        Change(rule_id="OPERATION_REMOVED", operation=n, path="", detail="operation removed")
        for n in sorted(b_ops.keys() - r_ops.keys())
    ]
    changes += [
        Change(rule_id="OPERATION_ADDED", operation=n, path="", detail="operation added")
        for n in sorted(r_ops.keys() - b_ops.keys())
    ]
    for name in sorted(b_ops.keys() & r_ops.keys()):
        b, r = b_ops[name], r_ops[name]
        alias = aliases.get(name, {})
        changes += _fields(name, "input", b.inputs, r.inputs, alias, rename_threshold)
        changes += _fields(name, "output", b.outputs, r.outputs, alias, rename_threshold)
        changes += [
            Change(rule_id="ERROR_REMOVED", operation=name, path=e, detail=f"error {e} removed")
            for e in sorted(b.errors - r.errors)
        ]
        changes += [
            Change(rule_id="ERROR_ADDED", operation=name, path=e, detail=f"error {e} added")
            for e in sorted(r.errors - b.errors)
        ]
    if not changes and base.content_hash() != rev.content_hash():
        changes.append(
            Change(rule_id="UNCLASSIFIED_CHANGE", operation="", path="", detail="shapes differ")
        )
    return changes


def _normalized(path: str) -> str:
    return re.sub(r"[^a-z0-9]", "", path.lower())


def _similarity(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, _normalized(a), _normalized(b)).ratio()


def _fields(  # noqa: PLR0913, PLR0917 - one operation's side of a diff
    op: str,
    direction: str,
    base: Sequence[FieldShape],
    rev: Sequence[FieldShape],
    alias: Mapping[str, str],
    threshold: float,
) -> list[Change]:
    b = {f.path: f for f in base}
    r = {f.path: f for f in rev}
    removed = sorted(b.keys() - r.keys())
    added = sorted(r.keys() - b.keys())
    changes: list[Change] = []
    pairs: list[tuple[str, str, str]] = []

    for old in list(removed):
        new = alias.get(old)
        if new in added:
            pairs.append((old, new, "FIELD_RENAMED_KNOWN"))
            removed.remove(old)
            added.remove(new)

    candidates = sorted(
        (
            (_similarity(old, new), old, new)
            for old in removed
            for new in added
            if b[old].type is r[new].type
        ),
        reverse=True,
    )
    for score, old, new in candidates:
        if score >= threshold and old in removed and new in added:
            pairs.append((old, new, "FIELD_RENAMED_SUSPECTED"))
            removed.remove(old)
            added.remove(new)

    for old, new, rule in pairs:
        changes.append(
            Change(
                rule_id=rule,
                operation=op,
                path=f"{old}->{new}",
                detail=f"{direction} field renamed",
            )
        )
        changes += _compare(op, direction, b[old], r[new], f"{old}->{new}")
    for path in removed:
        rule = "OUTPUT_FIELD_REMOVED" if direction == "output" else "INPUT_FIELD_REMOVED"
        changes.append(
            Change(rule_id=rule, operation=op, path=path, detail=f"{direction} field removed")
        )
    for path in added:
        if direction == "output":
            rule = "OUTPUT_FIELD_ADDED"
        else:
            rule = (
                "INPUT_REQUIRED_FIELD_ADDED" if r[path].required else "INPUT_OPTIONAL_FIELD_ADDED"
            )
        changes.append(
            Change(rule_id=rule, operation=op, path=path, detail=f"{direction} field added")
        )
    for path in sorted(b.keys() & r.keys()):
        changes += _compare(op, direction, b[path], r[path], path)
    return changes


def _compare(op: str, direction: str, a: FieldShape, b: FieldShape, path: str) -> list[Change]:
    out = direction == "output"
    found: list[tuple[str, str]] = []
    if a.type is not b.type:
        found.append(("TYPE_CHANGED", f"{a.type.value} -> {b.type.value}"))
    if a.required != b.required:
        if out:
            found.append(("OUTPUT_BECAME_REQUIRED" if b.required else "OUTPUT_BECAME_OPTIONAL", ""))
        else:
            found.append(("INPUT_BECAME_REQUIRED" if b.required else "INPUT_BECAME_OPTIONAL", ""))
    if a.nullable != b.nullable:
        side = "OUTPUT" if out else "INPUT"
        found.append(
            (f"{side}_BECAME_NULLABLE" if b.nullable else f"{side}_BECAME_NON_NULLABLE", "")
        )
    if a.enum_values is not None and b.enum_values is not None:
        side = "OUTPUT" if out else "INPUT"
        found += [(f"{side}_ENUM_VALUE_ADDED", v) for v in sorted(b.enum_values - a.enum_values)]
        found += [(f"{side}_ENUM_VALUE_REMOVED", v) for v in sorted(a.enum_values - b.enum_values)]
    if a.format != b.format:
        found.append(("FORMAT_CHANGED", f"{a.format} -> {b.format}"))
    return [
        Change(rule_id=rule, operation=op, path=path, detail=detail or rule.lower())
        for rule, detail in found
    ]


def classify(
    changes: Sequence[Change],
    book: RuleBook,
    check: CheckKind,
    *,
    inferred: bool,
    minor_bump: bool = False,
) -> list[Finding]:
    findings: list[Finding] = []
    for change in changes:
        rule = book.get(change.rule_id)
        classification = rule.classification(check) or Classification.UNKNOWN
        note = None
        if check is CheckKind.CANONICAL and rule.compatible_with_minor_bump and minor_bump:
            classification, note = Classification.COMPATIBLE, "MINOR version bump"
        if inferred and rule.presence and classification is Classification.COMPATIBLE:
            classification = Classification.REVIEW_REQUIRED
            note = "INFERRED provenance: presence evidence is weak"
        findings.append(Finding(**change.model_dump(), classification=classification, note=note))
    return findings

"""Extractors (brief §5.3): turn samples or a declared layout into a ``Shape``.

- ``observed``: JSON response bodies. Required = present in every parent object; nullable =
  null ever seen. Provenance is INFERRED until enough samples over enough days (M4-Q1).
- ``file``: a declared CSV layout (DECLARED), or CSV samples read by header name (OBSERVED).
Any ambiguity (conflicting types, mixed date formats, unreadable header) raises
``ExtractionError``, which the check reports as SOURCE_UNPARSEABLE (UNKNOWN, fail closed).
"""

import csv
import io
import re
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import datetime, timedelta

from pydantic import AwareDatetime, Field

from adapter_kernel.jsontypes import JsonValue
from adapter_kernel.shape import (
    FieldShape,
    FieldType,
    OperationShape,
    Provenance,
    ProvenanceKind,
    Shape,
    SourceKind,
)
from adapter_verify.common.model import FrozenModel

OBSERVED_EXTRACTOR = "observed"
FILE_EXTRACTOR = "file"
EXTRACTOR_VERSION = "1"

_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})")
_US = re.compile(r"\d{2}/\d{2}/\d{4} \d{2}:\d{2}:\d{2}")
_INT = re.compile(r"-?\d+")
_NUM = re.compile(r"-?\d+\.\d+")
_MIN_COLUMNS = 2


class ExtractionError(ValueError):
    """The samples cannot be reduced to one unambiguous shape."""


class Sample(FrozenModel):
    captured_at: AwareDatetime
    body: JsonValue


class ColumnSpec(FrozenModel):
    name: str = Field(min_length=1)
    type: FieldType
    required: bool = True
    nullable: bool = False
    enum_values: tuple[str, ...] | None = None
    format: str | None = None


class EvidenceThreshold(FrozenModel):
    min_samples: int = Field(ge=1)
    min_days: int = Field(ge=0)


class _Stats:
    def __init__(self) -> None:
        self.types: dict[str, set[FieldType]] = defaultdict(set)
        self.formats: dict[str, set[str]] = defaultdict(set)
        self.enums: dict[str, set[str]] = defaultdict(set)
        self.nulls: set[str] = set()
        self.present: dict[str, int] = defaultdict(int)
        self.parents: dict[str, int] = defaultdict(int)
        self.parent_of: dict[str, str] = {}


def _scalar(value: JsonValue) -> tuple[FieldType, str | None]:
    if isinstance(value, bool):
        return FieldType.BOOLEAN, None
    if isinstance(value, int):
        return FieldType.INTEGER, None
    if isinstance(value, float):
        return FieldType.NUMBER, None
    if isinstance(value, str):
        if _ISO.fullmatch(value):
            return FieldType.DATETIME, "ISO"
        if _US.fullmatch(value):
            return FieldType.DATETIME, "US"
        return FieldType.STRING, None
    msg = "unsupported value"
    raise ExtractionError(msg)


def _walk(stats: _Stats, value: JsonValue, path: str, enum_paths: frozenset[str]) -> None:
    if value is None:
        stats.nulls.add(path)
        return
    if isinstance(value, dict):
        if path:
            stats.types[path].add(FieldType.OBJECT)
        stats.parents[path] += 1
        for key, child in value.items():
            child_path = f"{path}.{key}" if path else key
            stats.parent_of[child_path] = path
            stats.present[child_path] += 1
            _walk(stats, child, child_path, enum_paths)
        return
    if isinstance(value, list):
        stats.types[path].add(FieldType.ARRAY)
        item = f"{path}[]"
        for element in value:
            stats.parent_of[item] = path
            stats.present[item] += 1
            _walk(stats, element, item, enum_paths)
        return
    kind, fmt = _scalar(value)
    if path in enum_paths:
        if kind is not FieldType.STRING:
            msg = f"enum field {path} holds a non-string value"
            raise ExtractionError(msg)
        stats.types[path].add(FieldType.ENUM)
        stats.enums[path].add(str(value))
        return
    stats.types[path].add(kind)
    if fmt:
        stats.formats[path].add(fmt)


def _field(stats: _Stats, path: str) -> FieldShape:
    types = stats.types.get(path, set())
    if types == {FieldType.INTEGER, FieldType.NUMBER}:
        types = {FieldType.NUMBER}
    if len(types) > 1:
        msg = f"conflicting types at {path}"
        raise ExtractionError(msg)
    formats = stats.formats.get(path, set())
    if len(formats) > 1:
        msg = f"mixed formats at {path}"
        raise ExtractionError(msg)
    kind = next(iter(types)) if types else FieldType.NULL
    parent = stats.parent_of.get(path, "")
    # Array items are "required" when every array seen had at least one element.
    required = path.endswith("[]") or stats.present[path] >= stats.parents.get(parent, 0)
    enum = frozenset(stats.enums[path]) if kind is FieldType.ENUM else None
    return FieldShape(
        path=path,
        type=kind,
        required=required,
        nullable=path in stats.nulls or kind is FieldType.NULL,
        enum_values=enum,
        format=next(iter(formats)) if formats else None,
    )


def _provenance(
    captured: Sequence[datetime],
    threshold: EvidenceThreshold,
    extractor: str,
    extracted_at: datetime,
) -> Provenance:
    span = max(captured) - min(captured)
    strong = len(captured) >= threshold.min_samples and span >= timedelta(days=threshold.min_days)
    return Provenance(
        kind=ProvenanceKind.OBSERVED if strong else ProvenanceKind.INFERRED,
        sample_count=len(captured),
        extractor=extractor,
        extractor_version=EXTRACTOR_VERSION,
        extracted_at=extracted_at,
    )


def _shape_from(  # noqa: PLR0913 - the identifying fields of a shape
    records: Iterable[JsonValue],
    *,
    source_id: str,
    kind: SourceKind,
    version: str,
    operation: str,
    enum_paths: Iterable[str],
    provenance: Provenance,
) -> Shape:
    stats = _Stats()
    enums = frozenset(enum_paths)
    for record in records:
        if not isinstance(record, dict):
            msg = "each sample must be a JSON object"
            raise ExtractionError(msg)
        _walk(stats, record, "", enums)
    paths = sorted(stats.present)
    return Shape(
        source_id=source_id,
        source_kind=kind,
        source_version=version,
        operations=(
            OperationShape(
                name=operation,
                inputs=(),
                outputs=tuple(_field(stats, p) for p in paths),
                errors=frozenset(),
            ),
        ),
        provenance=provenance,
    )


def extract_observed(  # noqa: PLR0913 - the identifying fields of a shape
    samples: Sequence[Sample],
    *,
    source_id: str,
    version: str,
    operation: str,
    enum_paths: Iterable[str],
    threshold: EvidenceThreshold,
    extracted_at: datetime,
) -> Shape:
    if not samples:
        msg = "no samples"
        raise ExtractionError(msg)
    return _shape_from(
        (s.body for s in samples),
        source_id=source_id,
        kind=SourceKind.OBSERVED,
        version=version,
        operation=operation,
        enum_paths=enum_paths,
        provenance=_provenance(
            [s.captured_at for s in samples], threshold, OBSERVED_EXTRACTOR, extracted_at
        ),
    )


def _cell(text: str) -> JsonValue:
    if text == "":
        return None
    if _INT.fullmatch(text):
        return int(text)
    if _NUM.fullmatch(text):
        return float(text)
    return text


def parse_csv(text: str, *, delimiter: str, expected_columns: Sequence[str]) -> list[JsonValue]:
    """Rows as objects keyed by header name, so column order never matters."""
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = reader.fieldnames or []
    if len(header) < _MIN_COLUMNS or not set(header) & set(expected_columns):
        msg = "header cannot be read with the declared delimiter"
        raise ExtractionError(msg)
    return [{k: _cell(v or "") for k, v in row.items() if k is not None} for row in reader]


def extract_csv(  # noqa: PLR0913 - the identifying fields of a shape
    files: Sequence[tuple[datetime, str]],
    *,
    layout: Sequence[ColumnSpec],
    delimiter: str,
    source_id: str,
    version: str,
    operation: str,
    threshold: EvidenceThreshold,
    extracted_at: datetime,
) -> Shape:
    names = [c.name for c in layout]
    enums = [c.name for c in layout if c.type is FieldType.ENUM]
    rows: list[JsonValue] = []
    captured: list[datetime] = []
    for when, text in files:
        parsed = parse_csv(text, delimiter=delimiter, expected_columns=names)
        rows += parsed
        captured += [when] * len(parsed)
    if not rows:
        msg = "no rows"
        raise ExtractionError(msg)
    return _shape_from(
        rows,
        source_id=source_id,
        kind=SourceKind.FILE,
        version=version,
        operation=operation,
        enum_paths=enums,
        provenance=_provenance(captured, threshold, FILE_EXTRACTOR, extracted_at),
    )


def extract_layout(
    layout: Sequence[ColumnSpec],
    *,
    source_id: str,
    version: str,
    operation: str,
    extracted_at: datetime,
) -> Shape:
    return Shape(
        source_id=source_id,
        source_kind=SourceKind.FILE,
        source_version=version,
        operations=(
            OperationShape(
                name=operation,
                inputs=(),
                outputs=tuple(
                    FieldShape(
                        path=c.name,
                        type=c.type,
                        required=c.required,
                        nullable=c.nullable,
                        enum_values=frozenset(c.enum_values) if c.enum_values else None,
                        format=c.format,
                    )
                    for c in layout
                ),
                errors=frozenset(),
            ),
        ),
        provenance=Provenance(
            kind=ProvenanceKind.DECLARED,
            sample_count=None,
            extractor=FILE_EXTRACTOR,
            extractor_version=EXTRACTOR_VERSION,
            extracted_at=extracted_at,
        ),
    )

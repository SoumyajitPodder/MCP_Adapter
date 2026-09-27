"""Fail-closed, schema-driven redaction (brief §8.4).

Paths use the ``FieldShape.path`` convention: object keys joined by ``.``, array items as
``[]`` (``order.items[].sku``).

Rules:
- A leaf is shown only if its own path is annotated ``public`` or ``internal``. Annotations
  on containers do not propagate to children.
- Object keys are kept only when the schema knows them. Unknown keys (e.g. maps keyed by
  account number) may themselves be data, so they are dropped and counted.
- Keys containing a path separator (``.``, ``[``, ``]``) could alias a nested path, so data
  keys like that are dropped and counted, and schema property names like that are an error.
"""

from collections.abc import Iterable, Mapping
from typing import Final

from adapter_kernel.jsontypes import JsonObject, JsonValue
from adapter_kernel.tooldef import Sensitivity

UNANNOTATED_MASK: Final = "<redacted:unannotated>"
UNKNOWN_KEYS_FIELD: Final = "<redacted:unknown-keys>"
_MASKS: Final[Mapping[Sensitivity, str]] = {
    Sensitivity.PII: "<redacted:pii>",
    Sensitivity.SECRET: "<redacted:secret>",
}


_PATH_SEPARATORS: Final = frozenset(".[]")


def _is_plain_key(key: str) -> bool:
    return _PATH_SEPARATORS.isdisjoint(key)


def child_path(parent: str, key: str) -> str:
    return f"{parent}.{key}" if parent else key


def item_path(parent: str) -> str:
    return f"{parent}[]"


def _prefixes(path: str) -> set[str]:
    found = {path}
    for i, ch in enumerate(path):
        if ch == ".":
            found.add(path[:i])
        elif path.startswith("[]", i):
            found.update((path[:i], path[: i + 2]))
    found.discard("")
    return found


class RedactionPolicy:
    """Redaction rules for one schema. Build once per tool version, reuse per call.

    ``declared`` lists schema paths that exist but may be unannotated: their keys are kept
    and their values masked. Keys neither declared nor annotated are dropped.
    """

    def __init__(
        self, sensitivity: Mapping[str, Sensitivity], declared: Iterable[str] = ()
    ) -> None:
        self._sensitivity = dict(sensitivity)
        self._known: frozenset[str] = frozenset(
            prefix for path in (*self._sensitivity, *declared) for prefix in _prefixes(path)
        )

    @classmethod
    def from_schema(cls, schema: JsonObject) -> "RedactionPolicy":
        sensitivity, declared = _walk_schema(schema)
        return cls(sensitivity, declared)

    def redact(self, value: JsonValue) -> JsonValue:
        return self._redact(value, "")

    def _redact(self, value: JsonValue, path: str) -> JsonValue:
        if isinstance(value, dict):
            return self._redact_object(value, path)
        if isinstance(value, list):
            return [self._redact(item, item_path(path)) for item in value]
        level = self._sensitivity.get(path)
        if level is None:
            return UNANNOTATED_MASK
        if level.may_appear_unmasked:
            return value
        return _MASKS[level]

    def _redact_object(self, value: JsonObject, path: str) -> JsonObject:
        out: JsonObject = {}
        unknown = 0
        for key, child in value.items():
            sub = child_path(path, key)
            if _is_plain_key(key) and sub in self._known:
                out[key] = self._redact(child, sub)
            else:
                unknown += 1
        if unknown:
            out[UNKNOWN_KEYS_FIELD] = unknown
        return out


def sensitivity_map(schema: JsonObject) -> dict[str, Sensitivity]:
    """Collect ``x-sensitivity`` annotations from a JSON Schema's ``properties``/``items``.

    Other keywords (``$ref``, ``oneOf``, ...) are not followed, so fields under them stay
    unannotated and therefore masked. An unrecognized annotation value, or a property name
    containing ``.``, ``[`` or ``]``, is an error.
    """
    return _walk_schema(schema)[0]


def _walk_schema(schema: JsonObject) -> tuple[dict[str, Sensitivity], set[str]]:
    found: dict[str, Sensitivity] = {}
    declared: set[str] = set()
    allowed = {s.value for s in Sensitivity}

    def walk(node: JsonValue, path: str) -> None:
        if not isinstance(node, dict):
            return
        if path:
            declared.add(path)
        raw = node.get("x-sensitivity")
        if raw is not None:
            if not isinstance(raw, str) or raw not in allowed:
                msg = f"invalid x-sensitivity at {path or '<root>'}"
                raise ValueError(msg)
            found[path] = Sensitivity(raw)
        properties = node.get("properties")
        if isinstance(properties, dict):
            for key, child in properties.items():
                if not _is_plain_key(key):
                    msg = f"property name {key!r} at {path or '<root>'} contains . [ or ]"
                    raise ValueError(msg)
                walk(child, child_path(path, key))
        walk(node.get("items"), item_path(path))

    walk(schema, "")
    return found, declared

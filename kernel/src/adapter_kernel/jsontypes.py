"""JSON value aliases, so public signatures never need ``Any``."""

from pydantic import JsonValue

type JsonObject = dict[str, JsonValue]

__all__ = ["JsonObject", "JsonValue"]

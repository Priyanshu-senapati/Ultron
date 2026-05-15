"""Lightweight JSON-schema-style validation for tool arguments.

We don't need a full draft-07 validator — tool schemas are tiny and we
prefer a tight, readable subset to a 3rd-party dep. Supported shapes:

    {"type": "object", "properties": {...}, "required": [...]}
    {"type": "string", "minLength": N, "maxLength": M, "enum": [...]}
    {"type": "integer" | "number", "minimum": ..., "maximum": ...}
    {"type": "boolean"}
    {"type": "array", "items": {...}, "maxItems": N}

Each ``validate(value, schema)`` returns a list of error strings; empty
list means valid.
"""
from __future__ import annotations

from typing import Any


class SchemaError(ValueError):
    """Raised when a tool call's args fail schema validation."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors) if errors else "schema error")
        self.errors = errors


def validate(value: Any, schema: dict[str, Any], path: str = "") -> list[str]:
    """Validate ``value`` against ``schema``. Returns list of errors."""
    if not isinstance(schema, dict):
        return [f"{path or '<root>'}: invalid schema (not a dict)"]
    expected = schema.get("type")
    errors: list[str] = []
    if expected == "object":
        if not isinstance(value, dict):
            return [f"{path or '<root>'}: expected object, got {type(value).__name__}"]
        props = schema.get("properties", {}) or {}
        required = schema.get("required", []) or []
        for key in required:
            if key not in value:
                errors.append(f"{path}{key}: missing required field")
        for k, v in value.items():
            sub_schema = props.get(k)
            if sub_schema is not None:
                errors.extend(validate(v, sub_schema, f"{path}{k}."))
        if schema.get("additionalProperties") is False:
            for k in value.keys():
                if k not in props:
                    errors.append(f"{path}{k}: unexpected field")
    elif expected == "string":
        if not isinstance(value, str):
            errors.append(f"{path or '<root>'}: expected string, got {type(value).__name__}")
        else:
            mn = schema.get("minLength")
            mx = schema.get("maxLength")
            if mn is not None and len(value) < mn:
                errors.append(f"{path or '<root>'}: too short (< {mn})")
            if mx is not None and len(value) > mx:
                errors.append(f"{path or '<root>'}: too long (> {mx})")
            allowed = schema.get("enum")
            if allowed is not None and value not in allowed:
                errors.append(f"{path or '<root>'}: not in enum {allowed}")
    elif expected in ("integer", "number"):
        if expected == "integer":
            if not isinstance(value, int) or isinstance(value, bool):
                errors.append(f"{path or '<root>'}: expected integer")
                return errors
        else:
            if not isinstance(value, (int, float)) or isinstance(value, bool):
                errors.append(f"{path or '<root>'}: expected number")
                return errors
        mn = schema.get("minimum")
        mx = schema.get("maximum")
        if mn is not None and value < mn:
            errors.append(f"{path or '<root>'}: < {mn}")
        if mx is not None and value > mx:
            errors.append(f"{path or '<root>'}: > {mx}")
    elif expected == "boolean":
        if not isinstance(value, bool):
            errors.append(f"{path or '<root>'}: expected boolean")
    elif expected == "array":
        if not isinstance(value, list):
            errors.append(f"{path or '<root>'}: expected array, got {type(value).__name__}")
            return errors
        item_schema = schema.get("items")
        if item_schema:
            for i, item in enumerate(value):
                errors.extend(validate(item, item_schema, f"{path}[{i}]."))
        mx = schema.get("maxItems")
        if mx is not None and len(value) > mx:
            errors.append(f"{path or '<root>'}: > {mx} items")
    else:
        errors.append(f"{path or '<root>'}: unknown schema type {expected!r}")
    return errors

"""Small JSON Schema helpers used by tool argument validation."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any


def validate_json_schema(schema: Mapping[str, Any], value: object) -> str | None:
    return _validate_schema(dict(schema), value, path="")


def _validate_schema(schema: Mapping[str, Any], value: object, *, path: str) -> str | None:
    combinator_error = _validate_combinators(schema, value, path=path)
    if combinator_error is not None:
        return combinator_error

    expected = schema.get("type")
    if expected is not None and not _matches_type(value, expected):
        return f"{_path_label(path)} must be {_type_label(expected)}"

    enum = schema.get("enum")
    if isinstance(enum, list) and value not in enum:
        allowed = ", ".join(repr(item) for item in enum)
        return f"{_path_label(path)} must be one of: {allowed}"

    if _schema_type_includes(schema, "object") and isinstance(value, dict):
        error = _validate_object(schema, value, path=path)
        if error is not None:
            return error
    if _schema_type_includes(schema, "array") and isinstance(value, list):
        error = _validate_array(schema, value, path=path)
        if error is not None:
            return error
    if _schema_type_includes(schema, "string") and isinstance(value, str):
        if schema.get("x-telos-string-integer"):
            error = _validate_integer_string(schema, value, path=path)
            if error is not None:
                return error
        if schema.get("x-telos-string-boolean"):
            error = _validate_boolean_string(value, path=path)
            if error is not None:
                return error
        error = _validate_string(schema, value, path=path)
        if error is not None:
            return error
    if (
        (_schema_type_includes(schema, "integer") or _schema_type_includes(schema, "number"))
        and isinstance(value, (int, float))
        and not isinstance(value, bool)
    ):
        error = _validate_number(schema, value, path=path)
        if error is not None:
            return error
    return None


def _validate_combinators(
    schema: Mapping[str, Any],
    value: object,
    *,
    path: str,
) -> str | None:
    any_of = schema.get("anyOf")
    if isinstance(any_of, list):
        if _matches_any_schema(any_of, value, path=path):
            return None
        return f"{_path_label(path)} must match at least one schema"

    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        matches = _count_matching_schemas(one_of, value, path=path)
        if matches == 1:
            return None
        return f"{_path_label(path)} must match exactly one schema"
    return None


def _matches_any_schema(
    schemas: list[Any],
    value: object,
    *,
    path: str,
) -> bool:
    return any(
        isinstance(item, Mapping) and _validate_schema(item, value, path=path) is None
        for item in schemas
    )


def _count_matching_schemas(
    schemas: list[Any],
    value: object,
    *,
    path: str,
) -> int:
    return sum(
        1
        for item in schemas
        if isinstance(item, Mapping) and _validate_schema(item, value, path=path) is None
    )


def _validate_object(
    schema: Mapping[str, Any],
    value: dict[str, Any],
    *,
    path: str,
) -> str | None:
    required = schema.get("required") or []
    if isinstance(required, list):
        for name in required:
            field_name = str(name)
            if field_name not in value:
                return f"missing required field {_field_path(path, field_name)!r}"

    raw_properties = schema.get("properties") or {}
    properties = raw_properties if isinstance(raw_properties, Mapping) else {}
    property_names = schema.get("propertyNames")
    if isinstance(property_names, Mapping):
        for name in value:
            error = _validate_schema(
                property_names,
                str(name),
                path=_field_path(path, str(name)),
            )
            if error is not None:
                return error
    additional = schema.get("additionalProperties", True)
    for name, item in value.items():
        prop_schema = properties.get(name)
        if not isinstance(prop_schema, Mapping):
            if additional is False:
                return f"unexpected field {_field_path(path, name)!r}"
            if isinstance(additional, Mapping):
                error = _validate_schema(additional, item, path=_field_path(path, name))
                if error is not None:
                    return error
            continue
        error = _validate_schema(prop_schema, item, path=_field_path(path, name))
        if error is not None:
            return error
    return None


def _validate_array(
    schema: Mapping[str, Any],
    value: list[Any],
    *,
    path: str,
) -> str | None:
    min_items = schema.get("minItems")
    if isinstance(min_items, int) and len(value) < min_items:
        return f"{_path_label(path)} must contain at least {min_items} item(s)"
    max_items = schema.get("maxItems")
    if isinstance(max_items, int) and len(value) > max_items:
        return f"{_path_label(path)} must contain at most {max_items} item(s)"

    item_schema = schema.get("items")
    if isinstance(item_schema, Mapping):
        for index, item in enumerate(value):
            error = _validate_schema(item_schema, item, path=f"{path}[{index}]")
            if error is not None:
                return error
    return None


def _validate_string(
    schema: Mapping[str, Any],
    value: str,
    *,
    path: str,
) -> str | None:
    min_length = schema.get("minLength")
    if isinstance(min_length, int) and len(value) < min_length:
        return f"{_path_label(path)} must be at least {min_length} character(s)"
    max_length = schema.get("maxLength")
    if isinstance(max_length, int) and len(value) > max_length:
        return f"{_path_label(path)} must be at most {max_length} character(s)"
    pattern = schema.get("pattern")
    if isinstance(pattern, str) and re.search(pattern, value) is None:
        return f"{_path_label(path)} must match pattern {pattern!r}"
    if (
        isinstance(pattern, str)
        and (
            _schema_type_includes(schema, "integer")
            or _schema_type_includes(schema, "number")
        )
    ):
        error = _validate_numeric_string_bounds(schema, value, path=path)
        if error is not None:
            return error
    return None


def _validate_number(
    schema: Mapping[str, Any],
    value: int | float,
    *,
    path: str,
) -> str | None:
    minimum = schema.get("minimum")
    if isinstance(minimum, (int, float)) and value < minimum:
        return f"{_path_label(path)} must be >= {minimum}"
    maximum = schema.get("maximum")
    if isinstance(maximum, (int, float)) and value > maximum:
        return f"{_path_label(path)} must be <= {maximum}"
    return None


def _validate_integer_string(
    schema: Mapping[str, Any],
    value: str,
    *,
    path: str,
) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped)
    except ValueError:
        return f"{_path_label(path)} must be an integer string"
    if str(parsed) != stripped:
        return f"{_path_label(path)} must be an integer string"
    return _validate_number(schema, parsed, path=path)


def _validate_numeric_string_bounds(
    schema: Mapping[str, Any],
    value: str,
    *,
    path: str,
) -> str | None:
    stripped = value.strip()
    if not stripped:
        return None
    try:
        parsed = int(stripped) if _schema_type_includes(schema, "integer") else float(stripped)
    except ValueError:
        return None
    return _validate_number(schema, parsed, path=path)


def _validate_boolean_string(value: str, *, path: str) -> str | None:
    stripped = value.strip().lower()
    if not stripped:
        return None
    if stripped in {"1", "0", "true", "false", "yes", "no", "on", "off"}:
        return None
    return f"{_path_label(path)} must be a boolean string"


def _matches_type(value: Any, expected: object) -> bool:
    if isinstance(expected, list):
        return any(_matches_type(value, item) for item in expected)
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return True


def _schema_type_includes(schema: Mapping[str, Any], expected_type: str) -> bool:
    value = schema.get("type")
    if value is None:
        return expected_type == "object" and "properties" in schema
    if isinstance(value, list):
        return expected_type in value
    return value == expected_type


def _type_label(expected: object) -> str:
    if isinstance(expected, list):
        return " or ".join(str(item) for item in expected)
    return str(expected)


def _field_path(parent: str, name: str) -> str:
    return name if not parent else f"{parent}.{name}"


def _path_label(path: str) -> str:
    return f"field {path!r}" if path else "value"

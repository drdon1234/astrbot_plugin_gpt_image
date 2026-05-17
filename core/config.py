from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any, Mapping

def merge_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """Merge AstrBot-provided config onto schema-derived defaults."""
    raw = deepcopy(dict(raw or {}))
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_merge(merged, raw)
    return merged


def get_section(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    """Return a config section as a dict, or an empty dict for invalid shapes."""
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def string_list(value: Any) -> list[str]:
    """Normalize comma-separated strings or lists into a trimmed string list."""
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def configured_string_list(config: Mapping[str, Any], section: str, key: str) -> list[str]:
    """Read a configured string list, falling back to schema defaults when empty."""
    values = _dedupe_strings(string_list(get_section(config, section).get(key)))
    if values:
        return values
    return _dedupe_strings(string_list(get_section(DEFAULT_CONFIG, section).get(key)))


def bool_value(value: Any, fallback: bool = False) -> bool:
    """Parse permissive boolean config values with an explicit fallback."""
    if value is None or value == "":
        return fallback
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on", "enabled"}:
        return True
    if normalized in {"0", "false", "no", "off", "disabled"}:
        return False
    return fallback


def int_value(value: Any, fallback: int, minimum: int | None = None, maximum: int | None = None) -> int:
    """Parse an integer config value and clamp it to optional bounds."""
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        parsed = fallback
    if minimum is not None:
        parsed = max(minimum, parsed)
    if maximum is not None:
        parsed = min(maximum, parsed)
    return parsed


def _deep_merge(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        if key not in target:
            continue
        if isinstance(value, Mapping) and isinstance(target.get(key), dict):
            _deep_merge(target[key], value)
        else:
            target[key] = value


def _load_schema_defaults() -> dict[str, Any]:
    """Load plugin defaults directly from the AstrBot config schema."""
    schema_path = Path(__file__).resolve().parents[1] / "_conf_schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    return _defaults_from_schema_items(schema)


def _defaults_from_schema_items(items: Mapping[str, Any]) -> dict[str, Any]:
    defaults: dict[str, Any] = {}
    for key, node in items.items():
        if not isinstance(node, Mapping):
            continue
        defaults[key] = _default_from_schema_node(node)
    return defaults


def _default_from_schema_node(node: Mapping[str, Any]) -> Any:
    if "default" in node:
        return deepcopy(node["default"])
    node_type = str(node.get("type") or "").strip().lower()
    if node_type == "object":
        return _defaults_from_schema_items(_mapping(node.get("items")))
    if node_type == "list":
        return []
    if node_type == "bool":
        return False
    if node_type == "int":
        return 0
    return ""


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _dedupe_strings(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


DEFAULT_CONFIG: dict[str, Any] = _load_schema_defaults()

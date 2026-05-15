from __future__ import annotations

from copy import deepcopy
from typing import Any, Mapping


DEFAULT_CONFIG: dict[str, Any] = {
    "api": {
        "api_key": "",
        "base_url": "https://api.openai.com/v1",
        "request_timeout_seconds": 120,
        "max_concurrent_image_requests": 8,
    },
    "defaults": {
        "size_preset": "自动",
        "custom_size": "1024x1024",
        "quality": "高",
        "generate_count": 1,
        "edit_count": 1,
    },
    "quota": {
        "group": {
            "enabled": True,
            "window_minutes": 60,
            "max_images": 20,
        },
        "private": {
            "enabled": True,
            "window_minutes": 60,
            "max_images": 5,
        },
    },
    "permissions": {
        "admin_id": "",
        "whitelist": {
            "enable": False,
            "user": [],
            "group": [],
        },
        "blacklist": {
            "enable": False,
            "user": [],
            "group": [],
        },
    },
    "behavior": {
        "send_start_notice": True,
        "include_generation_summary": False,
    },
}


def merge_config(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    raw = deepcopy(dict(raw or {}))
    merged = deepcopy(DEFAULT_CONFIG)
    _deep_merge(merged, raw)
    return merged


def get_section(config: Mapping[str, Any], key: str) -> dict[str, Any]:
    value = config.get(key)
    return value if isinstance(value, dict) else {}


def string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def bool_value(value: Any, fallback: bool = False) -> bool:
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

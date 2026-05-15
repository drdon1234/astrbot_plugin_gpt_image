from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .options import SIZE_PRESET_ALIASES


@dataclass(frozen=True)
class ParsedCommand:
    prompt: str
    options: dict[str, Any] = field(default_factory=dict)
    show_help: bool = False


HELP_TOKENS = {"help", "帮助", "说明", "用法", "?"}


def parse_command_message(message: str, command_names: Iterable[str]) -> ParsedCommand:
    body = strip_command_prefix(message, command_names).strip()
    if not body:
        return ParsedCommand(prompt="")

    if _normalize_token(body) in HELP_TOKENS:
        return ParsedCommand(prompt="", show_help=True)

    options: dict[str, Any] = {}
    prompt, size = _split_trailing_size(body)
    if size:
        options["size"] = size

    return ParsedCommand(prompt=prompt.strip(), options=options)


def strip_command_prefix(message: str, command_names: Iterable[str]) -> str:
    text = str(message or "").strip()
    names = sorted({str(name).strip() for name in command_names if str(name).strip()}, key=len, reverse=True)
    for name in names:
        for prefix in ("/", ""):
            command = f"{prefix}{name}"
            if text == command:
                return ""
            if text.startswith(f"{command} "):
                return text[len(command) :].strip()
    return text


def _parse_size_token(token: str) -> str | None:
    normalized = _normalize_token(token)
    if normalized in {"auto", "自动"}:
        return "auto"
    preset = SIZE_PRESET_ALIASES.get(normalized)
    if preset and preset != "custom":
        return preset
    if not _looks_like_size(normalized):
        return None
    return normalized.replace("×", "x").replace("*", "x").replace("＊", "x")


def _looks_like_size(token: str) -> bool:
    return bool(re.fullmatch(r"[1-9]\d{2,4}\s*[x×*＊]\s*[1-9]\d{2,4}", token))


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("：", ":")


def _split_trailing_size(body: str) -> tuple[str, str | None]:
    text = str(body or "").strip()
    if not text:
        return "", None

    for pattern in (
        r"(?P<prompt>.*?)(?:^|\s)(?P<size>[1-9]\d{2,4}\s*[x×*＊]\s*[1-9]\d{2,4})(?:\s+\d+\s*[:：]\s*\d+\s+[124]k)?\s*$",
        r"(?P<prompt>.*?)(?:^|\s)(?P<size>(?:16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1)\s+[124]k)\s*$",
        r"(?P<prompt>.*?)(?:^|\s)(?P<size>auto|自动)\s*$",
    ):
        match = re.fullmatch(pattern, text, re.I)
        if not match:
            continue
        size = _parse_size_token(match.group("size"))
        if size:
            return match.group("prompt").strip(), size
    return text, None

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable


class CommandParseError(ValueError):
    pass


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

    tokens = body.split()
    if len(tokens) == 1 and _normalize_token(tokens[0]) in HELP_TOKENS:
        return ParsedCommand(prompt="", show_help=True)

    for token in tokens:
        if token.startswith("-"):
            raise CommandParseError("invalid command parameter style")

    options: dict[str, Any] = {}
    size = _parse_size_token(tokens[-1]) if tokens else None
    if size:
        options["size"] = size
        tokens.pop()

    return ParsedCommand(prompt=" ".join(tokens).strip(), options=options)


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
    if not _looks_like_size(normalized):
        return None
    return normalized.replace("×", "x").replace("*", "x").replace("＊", "x")


def _looks_like_size(token: str) -> bool:
    return bool(re.fullmatch(r"[1-9]\d{2,4}[x×*＊][1-9]\d{2,4}", token))


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower()

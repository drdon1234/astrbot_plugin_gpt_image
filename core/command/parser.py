from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Iterable

from .options import CHAT_SIZE_ALIASES


@dataclass(frozen=True)
class ParsedCommand:
    """Parsed user command body and options extracted from chat text."""

    prompt: str
    options: dict[str, Any] = field(default_factory=dict)
    show_help: bool = False


HELP_TOKENS = {"help", "帮助", "说明", "用法", "?"}
SEPARATOR_CHARS = (
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009"
    "\u200a\u200b\u200c\u200d\u200e\u200f\u2028\u2029\u3000\ufeff"
)
SEPARATOR_PATTERN = r"\s\u2000-\u200f\u2028\u2029\u3000\ufeff"


def parse_command_message(message: str, command_names: Iterable[str]) -> ParsedCommand:
    """Parse a trigger-prefixed message into prompt text and trailing size options."""
    body = extract_command_body(message, command_names)
    if body is None:
        return ParsedCommand(prompt="")
    body = body.strip()
    if not body:
        return ParsedCommand(prompt="")

    if _normalize_token(body) in HELP_TOKENS:
        return ParsedCommand(prompt="", show_help=True)

    options: dict[str, Any] = {}
    prompt, size = _split_trailing_size(body)
    if size:
        options["size"] = size

    return ParsedCommand(prompt=prompt.strip(), options=options)


def normalize_command_message(message: str, command_names: Iterable[str]) -> str | None:
    """Normalize platform-added reply or mention prefixes before trigger matching."""
    raw_text = str(message or "").strip()
    text = _strip_leading_command_noise(raw_text)
    if _strip_matching_command_prefix(text, command_names) is not None:
        return text

    for candidate in (raw_text, _strip_leading_cq_prefixes(raw_text), text):
        mentioned_text = _slice_command_after_leading_text_mention(candidate, command_names)
        if mentioned_text is not None:
            return mentioned_text
    return None


def extract_command_body(message: str, command_names: Iterable[str]) -> str | None:
    """Return message content after a matching trigger, or None when not triggered."""
    text = normalize_command_message(message, command_names)
    if text is None:
        return None
    return _strip_matching_command_prefix(text, command_names)


def _strip_matching_command_prefix(text: str, command_names: Iterable[str]) -> str | None:
    names = sorted({str(name).strip() for name in command_names if str(name).strip()}, key=len, reverse=True)
    for name in names:
        if text == name:
            return ""
        if text.startswith(name) and _has_command_boundary(text, len(name)):
            return text[len(name) :].strip()
    return None


def _has_command_boundary(text: str, index: int) -> bool:
    return index >= len(text) or _is_separator(text[index])


def _is_separator(char: str) -> bool:
    return char.isspace() or char in SEPARATOR_CHARS


def _strip_leading_command_noise(message: str) -> str:
    text = str(message or "").strip()
    while True:
        stripped = _strip_leading_cq_prefixes(text)
        stripped = _strip_simple_leading_text_mentions(stripped)
        stripped = stripped.strip()
        if stripped == text:
            return stripped
        text = stripped


def _strip_leading_cq_prefixes(text: str) -> str:
    return re.sub(r"^(?:\s*\[CQ:(?:reply|at),[^\]]*\]\s*)+", "", text, flags=re.I).strip()


def _strip_simple_leading_text_mentions(text: str) -> str:
    value = text
    while True:
        match = re.match(r"^@[^@\s]+\s+", value)
        if not match:
            return value
        value = value[match.end() :].strip()


def _slice_command_after_leading_text_mention(text: str, command_names: Iterable[str]) -> str | None:
    value = str(text or "").strip()
    if not value.startswith("@"):
        return None

    matches: list[tuple[int, str]] = []
    names = sorted({str(name).strip() for name in command_names if str(name).strip()}, key=len, reverse=True)
    for name in names:
        pattern = (
            rf"(^|[{SEPARATOR_PATTERN}])"
            rf"(?P<command>{re.escape(name)})"
            rf"(?=$|[{SEPARATOR_PATTERN}])"
        )
        for match in re.finditer(pattern, value):
            start = match.start("command")
            prefix = value[:start].strip()
            if prefix.startswith("@") and "\n" not in prefix and len(prefix) <= 80:
                matches.append((start, value[start:]))
    if not matches:
        return None
    matches.sort(key=lambda item: item[0])
    return matches[0][1].strip()


def _parse_size_token(token: str) -> str | None:
    normalized = _normalize_token(token)
    preset = CHAT_SIZE_ALIASES.get(normalized)
    if preset:
        return preset
    if not _looks_like_size(normalized):
        return None
    return re.sub(r"\s*[x×*＊]\s*", "x", normalized)


def _looks_like_size(token: str) -> bool:
    return bool(re.fullmatch(r"[1-9]\d{2,4}\s*[x×*＊]\s*[1-9]\d{2,4}", token))


def _normalize_token(token: str) -> str:
    return str(token or "").strip().lower().replace("：", ":")


def _split_trailing_size(body: str) -> tuple[str, str | None]:
    """Split only a legal trailing size from the prompt body."""
    text = str(body or "").strip()
    if not text:
        return "", None

    pixel_match = re.fullmatch(
        r"(?P<prompt>.*?)(?:^|\s)(?P<size>[1-9]\d{2,4}\s*[x×*＊]\s*[1-9]\d{2,4})\s*$",
        text,
        re.I,
    )
    if pixel_match:
        size = _parse_size_token(pixel_match.group("size"))
        if size:
            return pixel_match.group("prompt").strip(), size

    preset_match = re.fullmatch(
        r"(?P<prompt>.*?)(?:^|\s)(?P<size>(?:(?:16\s*[:：]\s*9|9\s*[:：]\s*16|1\s*[:：]\s*1)\s+[124]k|(?:横屏|竖屏|方图)\s*[124]k))\s*$",
        text,
        re.I,
    )
    if preset_match:
        prompt = preset_match.group("prompt").strip()
        if _prompt_ends_with_pixel_size(prompt):
            return text, None
        size = _parse_size_token(preset_match.group("size"))
        if size:
            return prompt, size
    return text, None


def _prompt_ends_with_pixel_size(prompt: str) -> bool:
    parts = str(prompt or "").strip().split()
    return bool(parts and _looks_like_size(parts[-1]))

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..config import int_value


IMAGE_SIZE_CONSTRAINTS = {
    "max_edge": 3840,
    "multiple": 16,
    "max_ratio": 3,
    "min_pixels": 655360,
    "max_pixels": 8294400,
}

POPULAR_IMAGE_SIZES = (
    "1280x720",
    "720x1280",
    "1024x1024",
    "2560x1440",
    "1440x2560",
    "2048x2048",
    "3840x2160",
    "2160x3840",
    "2880x2880",
)

SIZE_PRESET_VALUES = {
    "自动": "auto",
    "1280x720 16:9 1k": "1280x720",
    "720x1280 9:16 1k": "720x1280",
    "1024x1024 1:1 1k": "1024x1024",
    "2560x1440 16:9 2k": "2560x1440",
    "1440x2560 9:16 2k": "1440x2560",
    "2048x2048 1:1 2k": "2048x2048",
    "3840x2160 16:9 4k": "3840x2160",
    "2160x3840 9:16 4k": "2160x3840",
    "2880x2880 1:1 4k": "2880x2880",
    "16:9 1k": "1280x720",
    "9:16 1k": "720x1280",
    "1:1 1k": "1024x1024",
    "16:9 2k": "2560x1440",
    "9:16 2k": "1440x2560",
    "1:1 2k": "2048x2048",
    "16:9 4k": "3840x2160",
    "9:16 4k": "2160x3840",
    "1:1 4k": "2880x2880",
    "自定义": "custom",
}
SIZE_PRESET_OPTIONS = (
    "自动",
    "1280x720 16:9 1K",
    "720x1280 9:16 1K",
    "1024x1024 1:1 1K",
    "2560x1440 16:9 2K",
    "1440x2560 9:16 2K",
    "2048x2048 1:1 2K",
    "3840x2160 16:9 4K",
    "2160x3840 9:16 4K",
    "2880x2880 1:1 4K",
    "自定义",
)
SIZE_PRESET_ALIASES = {
    **SIZE_PRESET_VALUES,
    "auto": "auto",
    "custom": "custom",
    **{size: size for size in POPULAR_IMAGE_SIZES},
}

IMAGE_MODEL = "gpt-image-2"
DEFAULT_BACKGROUND = "opaque"
DEFAULT_OUTPUT_FORMAT = "png"
DEFAULT_CUSTOM_SIZE = "1024x1024"
OFFICIAL_MAX_OUTPUT_COUNT = 10

QUALITY_LABELS = ("自动", "低", "中", "高")
QUALITY_VALUES = {
    "自动": "auto",
    "低": "low",
    "中": "medium",
    "高": "high",
    "auto": "auto",
    "low": "low",
    "medium": "medium",
    "high": "high",
}


class OptionError(ValueError):
    def __init__(self, message: str, code: str = "INVALID_OPTIONS") -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ParsedImageSize:
    value: str
    auto: bool = False
    width: int | None = None
    height: int | None = None


@dataclass(frozen=True)
class ImageOptions:
    prompt: str
    model: str
    size: str
    quality: str
    background: str
    output_format: str
    input_fidelity: str
    count: int


def parse_image_size(value: Any) -> ParsedImageSize | None:
    option = (
        str(value or "")
        .strip()
        .lower()
        .replace("×", "x")
        .replace("*", "x")
        .replace("＊", "x")
        .replace(" ", "")
    )
    if option in {"auto", "自动"}:
        return ParsedImageSize("auto", auto=True)

    parts = option.split("x")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return None
    width, height = int(parts[0]), int(parts[1])
    if width <= 0 or height <= 0:
        return None
    return ParsedImageSize(f"{width}x{height}", width=width, height=height)


def validate_image_size(value: Any) -> tuple[bool, str, ParsedImageSize | None]:
    parsed = parse_image_size(value)
    if parsed is None:
        return False, "尺寸格式需要是 自动、auto 或 宽x高，例如 1536x1024。", None
    if parsed.auto:
        return True, "", parsed

    width = int(parsed.width or 0)
    height = int(parsed.height or 0)
    max_edge = max(width, height)
    min_edge = min(width, height)
    total_pixels = width * height

    if max_edge > IMAGE_SIZE_CONSTRAINTS["max_edge"]:
        return False, "尺寸单边不能超过 3840 像素。", parsed
    if width % IMAGE_SIZE_CONSTRAINTS["multiple"] != 0 or height % IMAGE_SIZE_CONSTRAINTS["multiple"] != 0:
        return False, "宽高都需要是 16 的倍数。", parsed
    if max_edge / min_edge > IMAGE_SIZE_CONSTRAINTS["max_ratio"]:
        return False, "宽高比不能超过 3:1。", parsed
    if total_pixels < IMAGE_SIZE_CONSTRAINTS["min_pixels"] or total_pixels > IMAGE_SIZE_CONSTRAINTS["max_pixels"]:
        return False, "总像素需要在 655360 到 8294400 之间。", parsed
    return True, "", parsed


def normalize_image_size(value: Any, fallback: str = "1024x1024") -> str:
    ok, reason, parsed = validate_image_size(value or fallback)
    if ok and parsed:
        return parsed.value
    raise OptionError(reason, "INVALID_SIZE")


def resolve_default_image_size(defaults: Mapping[str, Any]) -> str:
    preset = _normalize_config_text(defaults.get("size_preset") or "自动")
    resolved = SIZE_PRESET_ALIASES.get(preset)
    if resolved == "custom":
        return str(defaults.get("custom_size") or DEFAULT_CUSTOM_SIZE)
    if resolved:
        return resolved
    return str(defaults.get("size_preset") or "自动").strip()


def normalize_choice(value: Any, allowed: Mapping[str, str], fallback: str, label: str, labels: tuple[str, ...]) -> str:
    option = _normalize_config_text(value or fallback)
    if option in allowed:
        return allowed[option]
    allowed_text = ", ".join(labels)
    raise OptionError(f"{label} 只能是 {allowed_text}。")


def normalize_count(value: Any, fallback: int, max_count: int) -> int:
    try:
        parsed = int(str(value if value is not None else fallback).strip())
    except (TypeError, ValueError):
        raise OptionError("生成张数需要是整数。", "INVALID_COUNT") from None
    if parsed < 1 or parsed > max_count:
        raise OptionError(f"生成张数需要在 1 到 {max_count} 之间。", "INVALID_COUNT")
    return parsed


def normalize_image_options(
    prompt: str,
    raw_options: Mapping[str, Any],
    defaults: Mapping[str, Any],
    *,
    max_output_count: int = OFFICIAL_MAX_OUTPUT_COUNT,
) -> ImageOptions:
    prompt = str(prompt or "").strip()
    if not prompt:
        raise OptionError("请提供生图提示词。", "PROMPT_REQUIRED")

    size = normalize_image_size(raw_options.get("size"), resolve_default_image_size(defaults))
    quality = normalize_choice(
        raw_options.get("quality"),
        QUALITY_VALUES,
        str(defaults.get("quality") or "自动"),
        "quality",
        QUALITY_LABELS,
    )
    input_fidelity = "auto"
    fallback_count = int_value(defaults.get("count"), 1, 1, max_output_count)
    count = normalize_count(raw_options.get("count"), fallback_count, max_output_count)

    return ImageOptions(
        prompt=prompt,
        model=IMAGE_MODEL,
        size=size,
        quality=quality,
        background=DEFAULT_BACKGROUND,
        output_format=DEFAULT_OUTPUT_FORMAT,
        input_fidelity=input_fidelity,
        count=count,
    )


def _normalize_config_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("：", ":")

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
    "自定义": "custom",
}
CHAT_SIZE_ALIASES = {
    "16:9 1k": "1280x720",
    "横屏 1k": "1280x720",
    "横屏1k": "1280x720",
    "9:16 1k": "720x1280",
    "竖屏 1k": "720x1280",
    "竖屏1k": "720x1280",
    "1:1 1k": "1024x1024",
    "方图 1k": "1024x1024",
    "方图1k": "1024x1024",
    "16:9 2k": "2560x1440",
    "横屏 2k": "2560x1440",
    "横屏2k": "2560x1440",
    "9:16 2k": "1440x2560",
    "竖屏 2k": "1440x2560",
    "竖屏2k": "1440x2560",
    "1:1 2k": "2048x2048",
    "方图 2k": "2048x2048",
    "方图2k": "2048x2048",
    "16:9 4k": "3840x2160",
    "横屏 4k": "3840x2160",
    "横屏4k": "3840x2160",
    "9:16 4k": "2160x3840",
    "竖屏 4k": "2160x3840",
    "竖屏4k": "2160x3840",
    "1:1 4k": "2880x2880",
    "方图 4k": "2880x2880",
    "方图4k": "2880x2880",
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
    size: str
    quality: str
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
    if option == "auto":
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
        return False, "尺寸格式需要是宽x高，例如 1536x1024。", None
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
    resolved = SIZE_PRESET_VALUES.get(preset)
    if resolved == "custom":
        return str(defaults.get("custom_size") or DEFAULT_CUSTOM_SIZE)
    if resolved:
        return resolved
    raise OptionError("默认尺寸选项无效，请在插件配置中选择 自动、常用尺寸或自定义。", "INVALID_SIZE_PRESET")


def normalize_choice(value: Any, allowed: Mapping[str, str], fallback: str, label: str, labels: tuple[str, ...]) -> str:
    option = _normalize_config_text(value or fallback)
    if option in allowed:
        return allowed[option]
    allowed_text = ", ".join(labels)
    raise OptionError(f"{label} 只能是 {allowed_text}。")


def normalize_image_options(
    prompt: str,
    raw_options: Mapping[str, Any],
    defaults: Mapping[str, Any],
    *,
    max_output_count: int = OFFICIAL_MAX_OUTPUT_COUNT,
    is_edit: bool = False,
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
    count_key = "edit_count" if is_edit else "generate_count"
    count = int_value(defaults.get(count_key), 1, 1, max_output_count)

    return ImageOptions(
        prompt=prompt,
        size=size,
        quality=quality,
        count=count,
    )


def _normalize_config_text(value: Any) -> str:
    return str(value or "").strip().lower().replace("：", ":")

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, AsyncIterator

from .command.options import IMAGE_MODEL, OFFICIAL_MAX_OUTPUT_COUNT, ImageOptions, OptionError, normalize_image_options
from .command.parser import parse_command_message
from .config import configured_string_list, get_section, merge_config
from .constants import PARAMETER_USAGE_MESSAGE
from .errors import UserFacingError
from .media.image_generator import ImageGenerator
from .media.references import PreparedImage, ReferenceImageManager
from .policy.access import evaluate_access
from .policy.identity import identity_from_event, quota_scope
from .policy.quota import QuotaExceededError, QuotaLedger, QuotaLimit
from .storage.tempfiles import TempFileManager


@dataclass(frozen=True)
class TextReply:
    text: str


@dataclass(frozen=True)
class ImageReply:
    path: str


Reply = TextReply | ImageReply


class PreciseImageService:
    def __init__(self, raw_config: dict | None, data_dir: Path, log: Any) -> None:
        self.config = merge_config(raw_config or {})
        self.log = log
        self.data_dir = Path(data_dir)
        self._closing = False
        self.temp_files = TempFileManager(self.data_dir.name)
        self.quota = QuotaLedger(self.data_dir / "usage.json")
        self.references = ReferenceImageManager(self.config, self.temp_files, log)
        self.generator = ImageGenerator(self.config, self.temp_files)
        self.generation_command_names = configured_string_list(self.config, "trigger", "generation_keywords")

    async def generate(self, event: Any, message_text: str) -> AsyncIterator[Reply]:
        if self._closing:
            yield TextReply("插件正在停止，暂不接受新的生图请求。")
            return

        released = False
        try:
            command = parse_command_message(message_text, self.generation_command_names)
            if command.show_help:
                yield TextReply(self.parameter_usage_message())
                return

            identity = identity_from_event(event)
            access_decision = evaluate_access(get_section(self.config, "permissions"), identity)
            if not access_decision.allowed:
                yield TextReply(access_decision.reason)
                return

            if self._closing:
                yield TextReply("插件正在停止，暂不接受新的生图请求。")
                return

            sources = await self.references.sources_from_event(event)
            options = self._normalize_options(command.prompt, command.options, is_edit=bool(sources))
            scope, quota_key, quota_limit = quota_scope(identity, self.config)
            if access_decision.is_admin:
                quota_limit = _unlimited_quota_limit(quota_limit)
            reservation = await self.quota.reserve(
                scope=scope,
                key=quota_key,
                cost=options.count,
                limit=quota_limit,
                metadata={
                    "user_id": identity.user_id,
                    "group_id": identity.group_id,
                    "model": IMAGE_MODEL,
                    "prompt_hash": hashlib.sha256(options.prompt.encode("utf-8")).hexdigest()[:16],
                },
            )
        except OptionError as error:
            if getattr(error, "code", "") in {"PROMPT_REQUIRED", "INVALID_SIZE"}:
                yield TextReply(self.parameter_usage_message())
                return
            yield TextReply(str(error))
            return
        except UserFacingError as error:
            yield TextReply(str(error))
            return
        except QuotaExceededError as error:
            yield TextReply(error.user_message())
            return
        except Exception as error:
            self.log.exception("Precise image command preparation failed: %s", error)
            yield TextReply("生图请求准备失败，请查看 AstrBot 日志。")
            return

        prepared_refs: list[PreparedImage] = []
        success_count = 0
        try:
            prepared_result = await self.references.prepare_images(sources)
            prepared_refs = prepared_result.images
            if sources and not prepared_refs:
                yield TextReply("检测到参考图，但没有可用图片。请确认图片可访问、大小未超限，并且不是本机或内网地址。")
                return
            if get_section(self.config, "behavior").get("send_start_notice", True):
                ref_text = f"，参考图 {len(prepared_refs)} 张" if prepared_refs else ""
                if prepared_result.skipped_count:
                    ref_text += f"，跳过 {prepared_result.skipped_count} 张不可用参考图"
                yield TextReply(f"开始生成 {options.count} 张图{ref_text}。")

            async for result_path in self.generator.generate_images(options, prepared_refs):
                success_count += 1
                try:
                    yield ImageReply(result_path)
                finally:
                    await self.temp_files.cleanup_file(result_path)

            if get_section(self.config, "behavior").get("include_generation_summary", True):
                released = await self._release_reservation(reservation, success_count)
                snapshot = await self.quota.snapshot(scope=scope, key=quota_key, limit=quota_limit)
                quota_text = (
                    "不限额"
                    if snapshot.remaining is None
                    else f"剩余 {snapshot.remaining} 张 / {snapshot.window_minutes} 分钟"
                )
                yield TextReply(f"完成：生成 {success_count}/{options.count} 张。模型 {IMAGE_MODEL}，尺寸 {options.size}，{quota_text}。")
        except UserFacingError as error:
            yield TextReply(str(error))
        except Exception as error:
            self.log.exception("Precise image generation failed: %s", error)
            yield TextReply("图片生成失败，请查看 AstrBot 日志。")
        finally:
            if not released:
                await self._release_reservation(reservation, success_count)
            await self.references.cleanup(prepared_refs)

    async def quota_status(self, event: Any) -> TextReply:
        try:
            identity = identity_from_event(event)
            access_decision = evaluate_access(get_section(self.config, "permissions"), identity)
            scope, key, quota_limit = quota_scope(identity, self.config)
            if access_decision.is_admin:
                quota_limit = _unlimited_quota_limit(quota_limit)
            snapshot = await self.quota.snapshot(scope=scope, key=key, limit=quota_limit)
            if snapshot.remaining is None:
                return TextReply("当前会话生图配额：不限额。")
            scope_label = "私聊" if scope == "private" else "群聊"
            return TextReply(
                f"当前{scope_label}配额：{snapshot.window_minutes} 分钟最多 {snapshot.limit} 张，"
                f"已用 {snapshot.used} 张，进行中 {snapshot.active} 张，剩余 {snapshot.remaining} 张。"
            )
        except Exception as error:
            self.log.exception("Precise image quota status failed: %s", error)
            return TextReply("读取生图配额失败，请查看 AstrBot 日志。")

    def _normalize_options(self, prompt: str, raw_options: dict[str, Any], *, is_edit: bool = False) -> ImageOptions:
        defaults = dict(get_section(self.config, "defaults"))
        return normalize_image_options(
            prompt,
            raw_options,
            defaults,
            max_output_count=OFFICIAL_MAX_OUTPUT_COUNT,
            is_edit=is_edit,
        )

    def begin_shutdown(self) -> None:
        self._closing = True

    def parameter_usage_message(self) -> str:
        triggers = "、".join(self.generation_command_names)
        return f"{PARAMETER_USAGE_MESSAGE}\n当前触发词：{triggers}"

    async def shutdown(self) -> None:
        self.begin_shutdown()
        await self.temp_files.cleanup_all()

    async def cleanup_reply(self, reply: Reply) -> None:
        if isinstance(reply, ImageReply):
            await self.temp_files.cleanup_file(reply.path)

    async def _release_reservation(self, reservation: Any, success_count: int) -> bool:
        try:
            await reservation.release(success_count=success_count)
            return True
        except Exception as error:
            self.log.exception("Precise image quota release failed: %s", error)
            return False


def _unlimited_quota_limit(limit: QuotaLimit) -> QuotaLimit:
    return QuotaLimit(enabled=False, window_minutes=limit.window_minutes, max_images=0)

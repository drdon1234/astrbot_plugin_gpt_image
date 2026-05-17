from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register
from astrbot.core.utils.astrbot_path import get_astrbot_data_path
from astrbot.core.star.filter.event_message_type import EventMessageType

from .core.command.parser import extract_command_body, normalize_command_message
from .core.constants import PLUGIN_NAME, STATUS_COMMAND_NAMES
from .core.service import ImageReply, PreciseImageService, TextReply
from .core.storage.paths import resolve_plugin_data_dir


@register(PLUGIN_NAME, "drdon1234", "基于 gpt-image-2 的精细生图和引用图改图插件。", "0.1.5")
class PreciseImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        data_dir = resolve_plugin_data_dir(PLUGIN_NAME, get_astrbot_data_path)
        self.service = PreciseImageService(config or {}, data_dir, logger)
        self._active_generation_tasks: set[asyncio.Task] = set()

    @filter.event_message_type(EventMessageType.ALL)
    async def handle_image_message(self, event: AstrMessageEvent):
        if self._is_event_stopped(event):
            return

        message_text = self._command_message_text(event)
        if message_text is None:
            return

        self._stop_event(event)
        status_body = extract_command_body(message_text, STATUS_COMMAND_NAMES)
        if status_body is not None:
            reply = await self.service.quota_status(event)
            await event.send(self._to_astrbot_result(event, reply))
            return

        task = asyncio.current_task()
        if task:
            self._active_generation_tasks.add(task)
        try:
            async for reply in self.service.generate(event, message_text):
                if isinstance(reply, ImageReply):
                    try:
                        await event.send(self._to_astrbot_result(event, reply))
                    finally:
                        await self.service.cleanup_reply(reply)
                    continue
                await event.send(self._to_astrbot_result(event, reply))
        finally:
            if task:
                self._active_generation_tasks.discard(task)

    async def terminate(self):
        self.service.begin_shutdown()
        current = asyncio.current_task()
        tasks = [
            task
            for task in self._active_generation_tasks
            if task is not current and not task.done()
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            try:
                await asyncio.wait_for(asyncio.gather(*tasks, return_exceptions=True), timeout=15)
            except asyncio.TimeoutError:
                logger.warning("Precise image plugin shutdown timed out while cancelling %s generation task(s).", len(tasks))
        await self.service.shutdown()
        return None

    def _to_astrbot_result(self, event: AstrMessageEvent, reply: TextReply | ImageReply):
        if isinstance(reply, ImageReply):
            return event.image_result(reply.path)
        return event.plain_result(reply.text)

    def _command_message_text(self, event: AstrMessageEvent) -> str | None:
        command_names = [*STATUS_COMMAND_NAMES, *self.service.generation_command_names]
        for text in self._candidate_message_texts(event):
            normalized = normalize_command_message(text, command_names)
            if normalized is not None:
                return normalized
        return None

    def _candidate_message_texts(self, event: AstrMessageEvent) -> list[str]:
        candidates: list[str] = []
        for chain in self._message_chains_from_event(event):
            text = self._text_from_chain_without_leading_mentions(chain)
            if text:
                candidates.append(text)
        if not candidates:
            candidates.append(str(getattr(event, "message_str", "") or ""))
        return candidates

    def _message_chains_from_event(self, event: AstrMessageEvent) -> list[list[object]]:
        chains: list[list[object]] = []
        seen: set[int] = set()
        for chain in (
            self._safe_get_messages(event),
            getattr(getattr(event, "message_obj", None), "message", []),
        ):
            if not isinstance(chain, list):
                continue
            marker = id(chain)
            if marker in seen:
                continue
            seen.add(marker)
            chains.append(chain)
        return chains

    def _safe_get_messages(self, event: AstrMessageEvent) -> list[object]:
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return []
        try:
            return list(getter() or [])
        except Exception:
            return []

    def _text_from_chain_without_leading_mentions(self, chain: list[object]) -> str:
        parts: list[str] = []
        skipping_prefix = True
        for component in chain:
            kind = self._component_kind(component)
            kind_parts = kind.split()
            if skipping_prefix and (
                "reply" in kind
                or "at" in kind_parts
                or "mention" in kind
            ):
                continue
            text = self._component_text(component)
            if not text:
                continue
            parts.append(text)
            if text.strip():
                skipping_prefix = False
        return "".join(parts).strip()

    def _component_kind(self, component: object) -> str:
        class_name = component.__class__.__name__.lower()
        segment_type = str(getattr(component, "type", "") or class_name).lower()
        return f"{segment_type} {class_name}"

    def _component_text(self, component: object) -> str:
        for attr in ("text", "message_str", "content"):
            value = getattr(component, attr, None)
            if isinstance(value, str):
                return value
        data = getattr(component, "data", None)
        if isinstance(data, dict):
            for key in ("text", "message_str", "content"):
                value = data.get(key)
                if isinstance(value, str):
                    return value
        if isinstance(component, str):
            return component
        return ""

    def _stop_event(self, event: AstrMessageEvent) -> None:
        stopper = getattr(event, "stop_event", None)
        if callable(stopper):
            stopper()

    def _is_event_stopped(self, event: AstrMessageEvent) -> bool:
        checker = getattr(event, "is_stopped", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

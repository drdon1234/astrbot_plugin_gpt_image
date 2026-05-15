from __future__ import annotations

import asyncio

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

try:
    from astrbot.api.star import register
except ImportError:  # AstrBot 4 template mode can rely on metadata.yaml.
    def register(*_args, **_kwargs):  # type: ignore[no-redef]
        def decorator(cls):
            return cls

        return decorator

try:
    from astrbot.core.utils.astrbot_path import get_astrbot_data_path
except ImportError:
    get_astrbot_data_path = None  # type: ignore[assignment]

from .core.constants import PLUGIN_NAME
from .core.service import ImageReply, PreciseImageService, TextReply
from .core.storage.paths import resolve_plugin_data_dir


@register(PLUGIN_NAME, "Independent", "精细参数控制的 OpenAI-compatible 生图插件，支持群聊/私聊配额和黑白名单。", "0.1.3")
class PreciseImagePlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig | None = None):
        super().__init__(context)
        data_dir = resolve_plugin_data_dir(PLUGIN_NAME, get_astrbot_data_path)
        self.service = PreciseImageService(config or {}, data_dir, logger)
        self._active_generation_tasks: set[asyncio.Task] = set()

    @filter.command("gimg", alias={"生图", "画图"})
    async def generate_image(self, event: AstrMessageEvent):
        task = asyncio.current_task()
        if task:
            self._active_generation_tasks.add(task)
        try:
            async for reply in self.service.generate(event, event.message_str):
                if isinstance(reply, ImageReply):
                    try:
                        yield self._to_astrbot_result(event, reply)
                    finally:
                        await self.service.cleanup_reply(reply)
                    continue
                yield self._to_astrbot_result(event, reply)
        finally:
            if task:
                self._active_generation_tasks.discard(task)

    @filter.command("gimg_status", alias={"生图额度"})
    async def image_quota_status(self, event: AstrMessageEvent):
        yield self._to_astrbot_result(event, await self.service.quota_status(event))

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

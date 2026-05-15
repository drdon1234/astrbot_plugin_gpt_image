from __future__ import annotations

import base64
import contextlib
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from ..command.options import ImageOptions
from ..config import get_section, int_value
from ..constants import EXTENSION_BY_MIME, IMAGE_DOWNLOAD_TIMEOUT_SECONDS, IMAGE_MIME_TYPES
from ..errors import UserFacingError
from ..storage.tempfiles import TempFileManager
from .references import PreparedImage


class ImageGenerator:
    def __init__(self, config: dict, temp_files: TempFileManager) -> None:
        self.config = config
        self.temp_files = temp_files

    async def generate_images(self, options: ImageOptions, reference_images: list[PreparedImage]) -> AsyncIterator[str]:
        client = self._client()
        try:
            for _ in range(options.count):
                if reference_images:
                    response = await self._create_image_edit(client, options, reference_images)
                else:
                    response = await self._create_image_generation(client, options)
                yield await self._materialize_first_image(response, options.output_format)
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    def _client(self) -> AsyncOpenAI:
        api = get_section(self.config, "api")
        api_key = str(api.get("api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise UserFacingError("插件未配置 API Key。请在插件配置中填写 api.api_key，或设置 OPENAI_API_KEY。")
        base_url = str(api.get("base_url") or "https://api.openai.com/v1").strip()
        timeout = int_value(api.get("request_timeout_seconds"), 120, 10, 600)
        return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def _create_image_generation(self, client: AsyncOpenAI, options: ImageOptions) -> Any:
        request = {
            "model": options.model,
            "prompt": options.prompt,
            "size": options.size,
            "quality": options.quality,
            "n": 1,
        }
        if options.background != "auto":
            request["background"] = options.background
        if options.output_format != "png":
            request["output_format"] = options.output_format
        return await client.images.generate(**request)

    async def _create_image_edit(
        self,
        client: AsyncOpenAI,
        options: ImageOptions,
        reference_images: list[PreparedImage],
    ) -> Any:
        handles = []
        try:
            for image in reference_images:
                handles.append(image.path.open("rb"))
            request = {
                "model": options.model,
                "prompt": options.prompt,
                "image": handles[0] if len(handles) == 1 else handles,
                "size": options.size,
                "quality": options.quality,
                "n": 1,
            }
            if options.background != "auto":
                request["background"] = options.background
            if options.output_format != "png":
                request["output_format"] = options.output_format
            if options.input_fidelity != "auto" and not _omits_input_fidelity(options.model):
                request["input_fidelity"] = options.input_fidelity
            return await client.images.edit(**request)
        finally:
            for handle in handles:
                handle.close()

    async def _materialize_first_image(self, response: Any, output_format: str) -> str:
        data = getattr(response, "data", None) or []
        if not data:
            raise UserFacingError("图片接口没有返回可显示的图片。")
        image = data[0]
        b64_json = getattr(image, "b64_json", None) or _mapping_get(image, "b64_json") or _mapping_get(image, "b64Json")
        if b64_json:
            return str(self._store_generated_payload(str(b64_json), output_format))
        url = getattr(image, "url", None) or _mapping_get(image, "url")
        if url:
            return str(await self._download_generated_url(str(url), output_format))
        raise UserFacingError("图片接口没有返回 b64_json 或 url。")

    def _store_generated_payload(self, payload: str, output_format: str) -> Path:
        value = str(payload or "").strip()
        mime = f"image/{output_format if output_format != 'jpg' else 'jpeg'}"
        data_url_match = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,(.+)$", value, re.I | re.S)
        if data_url_match:
            mime = data_url_match.group(1).lower().replace("image/jpg", "image/jpeg")
            value = data_url_match.group(2)
        data = base64.b64decode(re.sub(r"\s+", "", value))
        if not data:
            raise UserFacingError("生成图片为空。")
        extension = EXTENSION_BY_MIME.get(mime, output_format)
        return self.temp_files.write_bytes(data, label="generated", extension=extension)

    async def _download_generated_url(self, url: str, output_format: str) -> Path:
        async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            mime = response.headers.get("content-type", f"image/{output_format}").split(";", 1)[0].lower()
            mime = mime.replace("image/jpg", "image/jpeg")
            if mime not in IMAGE_MIME_TYPES:
                raise UserFacingError("生成图片下载结果不是支持的图片格式。")
            if not response.content:
                raise UserFacingError("生成图片下载为空。")
            extension = EXTENSION_BY_MIME.get(mime, output_format)
            return self.temp_files.write_bytes(response.content, label="generated", extension=extension)


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    with contextlib.suppress(Exception):
        return value[key]
    return None


def _omits_input_fidelity(model: str) -> bool:
    return str(model or "").strip().lower().startswith("gpt-image-2")

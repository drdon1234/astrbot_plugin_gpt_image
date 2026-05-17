from __future__ import annotations

import asyncio
import base64
import contextlib
import os
import re
from pathlib import Path
from typing import Any, AsyncIterator

import httpx
from openai import AsyncOpenAI

from ..command.options import DEFAULT_BACKGROUND, DEFAULT_OUTPUT_FORMAT, IMAGE_MODEL, ImageOptions
from ..config import get_section, int_value
from ..constants import EXTENSION_BY_MIME, IMAGE_DOWNLOAD_TIMEOUT_SECONDS, IMAGE_MIME_TYPES
from ..errors import UserFacingError
from ..storage.tempfiles import TempFileManager
from .references import PreparedImage


class ImageGenerator:
    """Client wrapper for gpt-image-2 generation, editing, and result materialization."""

    def __init__(self, config: dict, temp_files: TempFileManager) -> None:
        self.config = config
        self.temp_files = temp_files
        self._request_semaphore = asyncio.Semaphore(_max_concurrent_image_requests(config))

    async def generate_images(self, options: ImageOptions, reference_images: list[PreparedImage]) -> AsyncIterator[str]:
        """Generate the requested number of images as concurrent single-image requests."""
        client = self._client()
        tasks: list[asyncio.Task[str]] = []
        try:
            tasks = [
                asyncio.create_task(self._generate_one_image(client, options, reference_images))
                for _ in range(options.count)
            ]
            for task in asyncio.as_completed(tasks):
                yield await task
        except BaseException:
            for task in tasks:
                task.cancel()
            with contextlib.suppress(BaseException):
                await asyncio.gather(*tasks, return_exceptions=True)
            raise
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    async def _generate_one_image(
        self,
        client: AsyncOpenAI,
        options: ImageOptions,
        reference_images: list[PreparedImage],
    ) -> str:
        async with self._request_semaphore:
            if reference_images:
                response = await self._create_image_edit(client, options, reference_images)
            else:
                response = await self._create_image_generation(client, options)
        return await self._materialize_first_image(response)

    def _client(self) -> AsyncOpenAI:
        """Create an OpenAI-compatible async client from plugin config."""
        api = get_section(self.config, "api")
        api_key = str(api.get("api_key") or os.getenv("OPENAI_API_KEY") or "").strip()
        if not api_key:
            raise UserFacingError("插件未配置 API Key。请在插件配置中填写 api.api_key，或设置 OPENAI_API_KEY。")
        base_url = str(api.get("base_url") or "https://api.openai.com/v1").strip()
        timeout = int_value(api.get("request_timeout_seconds"), 120, 10, 600)
        return AsyncOpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    async def _create_image_generation(self, client: AsyncOpenAI, options: ImageOptions) -> Any:
        """Send a single text-to-image request."""
        request = {
            "model": IMAGE_MODEL,
            "prompt": options.prompt,
            "size": options.size,
            "quality": options.quality,
            "n": 1,
        }
        if DEFAULT_BACKGROUND != "auto":
            request["background"] = DEFAULT_BACKGROUND
        if DEFAULT_OUTPUT_FORMAT != "png":
            request["output_format"] = DEFAULT_OUTPUT_FORMAT
        return await client.images.generate(**request)

    async def _create_image_edit(
        self,
        client: AsyncOpenAI,
        options: ImageOptions,
        reference_images: list[PreparedImage],
    ) -> Any:
        """Send a single image-edit request with prepared reference images."""
        handles = []
        try:
            for image in reference_images:
                handles.append(image.path.open("rb"))
            request = {
                "model": IMAGE_MODEL,
                "prompt": options.prompt,
                "image": handles[0] if len(handles) == 1 else handles,
                "size": options.size,
                "quality": options.quality,
                "n": 1,
            }
            if DEFAULT_BACKGROUND != "auto":
                request["background"] = DEFAULT_BACKGROUND
            if DEFAULT_OUTPUT_FORMAT != "png":
                request["output_format"] = DEFAULT_OUTPUT_FORMAT
            return await client.images.edit(**request)
        finally:
            for handle in handles:
                handle.close()

    async def _materialize_first_image(self, response: Any) -> str:
        """Store the first image from a b64_json or URL response and return its path."""
        data = getattr(response, "data", None) or []
        if not data:
            raise UserFacingError("图片接口没有返回可显示的图片。")
        image = data[0]
        b64_json = getattr(image, "b64_json", None) or _mapping_get(image, "b64_json") or _mapping_get(image, "b64Json")
        if b64_json:
            return str(self._store_generated_payload(str(b64_json)))
        url = getattr(image, "url", None) or _mapping_get(image, "url")
        if url:
            return str(await self._download_generated_url(str(url)))
        raise UserFacingError("图片接口没有返回 b64_json 或 url。")

    def _store_generated_payload(self, payload: str) -> Path:
        """Decode a base64 or data URL image payload into plugin temporary storage."""
        value = str(payload or "").strip()
        mime = f"image/{DEFAULT_OUTPUT_FORMAT if DEFAULT_OUTPUT_FORMAT != 'jpg' else 'jpeg'}"
        data_url_match = re.match(r"^data:(image/(?:png|jpeg|jpg|webp));base64,(.+)$", value, re.I | re.S)
        if data_url_match:
            mime = data_url_match.group(1).lower().replace("image/jpg", "image/jpeg")
            value = data_url_match.group(2)
        data = base64.b64decode(re.sub(r"\s+", "", value))
        if not data:
            raise UserFacingError("生成图片为空。")
        extension = EXTENSION_BY_MIME.get(mime, DEFAULT_OUTPUT_FORMAT)
        return self.temp_files.write_bytes(data, label="generated", extension=extension)

    async def _download_generated_url(self, url: str) -> Path:
        """Download a generated image URL into plugin temporary storage."""
        async with httpx.AsyncClient(timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS, follow_redirects=True) as client:
            response = await client.get(url)
            response.raise_for_status()
            mime = response.headers.get("content-type", f"image/{DEFAULT_OUTPUT_FORMAT}").split(";", 1)[0].lower()
            mime = mime.replace("image/jpg", "image/jpeg")
            if mime not in IMAGE_MIME_TYPES:
                raise UserFacingError("生成图片下载结果不是支持的图片格式。")
            if not response.content:
                raise UserFacingError("生成图片下载为空。")
            extension = EXTENSION_BY_MIME.get(mime, DEFAULT_OUTPUT_FORMAT)
            return self.temp_files.write_bytes(response.content, label="generated", extension=extension)


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, dict):
        return value.get(key)
    with contextlib.suppress(Exception):
        return value[key]
    return None


def _max_concurrent_image_requests(config: dict) -> int:
    api = get_section(config, "api")
    return int_value(api.get("max_concurrent_image_requests"), 2, 1, 16)

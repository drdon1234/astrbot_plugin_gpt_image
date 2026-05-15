from __future__ import annotations

import asyncio
import base64
import contextlib
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin, urlsplit

import httpx

from ..constants import (
    EXTENSION_BY_MIME,
    IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
    IMAGE_MIME_TYPES,
    MAX_REFERENCE_BYTES,
    MAX_REFERENCE_IMAGES,
)
from ..errors import UserFacingError
from ..storage.tempfiles import TempFileManager


@dataclass(frozen=True)
class PreparedImage:
    path: Path
    source: str
    mime_type: str
    temporary: bool = False


@dataclass(frozen=True)
class ReferencePreparationResult:
    images: list[PreparedImage]
    source_count: int
    attempted_count: int
    failed_count: int
    truncated_count: int

    @property
    def skipped_count(self) -> int:
        return self.failed_count + self.truncated_count


class ReferenceImageManager:
    def __init__(self, config: dict, temp_files: TempFileManager, log: Any) -> None:
        self.config = config
        self.temp_files = temp_files
        self.log = log

    async def sources_from_event(self, event: Any) -> list[str]:
        message_obj = getattr(event, "message_obj", None)
        chain = getattr(message_obj, "message", []) if message_obj else []
        raw_message = getattr(message_obj, "raw_message", None) if message_obj else None
        sources, reply_ids = self._extract_references_from_chain(chain)
        raw_sources, raw_reply_ids = self._extract_references_from_raw(raw_message)
        sources.extend(raw_sources)
        reply_ids.extend(raw_reply_ids)

        for reply_id in _dedupe(reply_ids):
            quoted = await self._fetch_quoted_message(event, reply_id)
            quoted_sources, nested_reply_ids = self._extract_sources_from_raw(quoted)
            sources.extend(quoted_sources)
            for nested_reply_id in nested_reply_ids:
                nested = await self._fetch_quoted_message(event, nested_reply_id)
                nested_sources, _ = self._extract_sources_from_raw(nested)
                sources.extend(nested_sources)

        return _dedupe(sources)

    async def prepare_images(self, sources: list[str]) -> ReferencePreparationResult:
        prepared: list[PreparedImage] = []
        limited_sources = sources[:MAX_REFERENCE_IMAGES]
        failed_count = 0
        for source in limited_sources:
            try:
                image = await self._prepare_image(
                    source,
                    max_bytes=MAX_REFERENCE_BYTES,
                    timeout=IMAGE_DOWNLOAD_TIMEOUT_SECONDS,
                )
                if image:
                    prepared.append(image)
                else:
                    failed_count += 1
            except Exception as error:
                failed_count += 1
                self.log.warning("Skipping unsupported reference image %s: %s", source, error)
        return ReferencePreparationResult(
            images=prepared,
            source_count=len(sources),
            attempted_count=len(limited_sources),
            failed_count=failed_count,
            truncated_count=max(0, len(sources) - len(limited_sources)),
        )

    async def cleanup(self, images: list[PreparedImage]) -> None:
        for image in images:
            if image.temporary:
                await self.temp_files.cleanup_file(image.path)

    def _extract_references_from_chain(self, chain: Iterable[Any]) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        for segment in chain or []:
            class_name = segment.__class__.__name__.lower()
            segment_type = str(getattr(segment, "type", "") or class_name).lower()
            if "reply" not in segment_type and "reply" not in class_name:
                continue

            reply_id = str(
                getattr(segment, "id", "")
                or getattr(segment, "message_id", "")
                or getattr(segment, "msg_id", "")
                or ""
            ).strip()
            if reply_id:
                reply_ids.append(reply_id)
            for attr in ("message", "chain", "message_chain", "content"):
                nested = getattr(segment, attr, None)
                if isinstance(nested, list):
                    nested_sources, nested_reply_ids = self._extract_sources_from_chain(nested)
                    sources.extend(nested_sources)
                    reply_ids.extend(nested_reply_ids)
        return sources, reply_ids

    def _extract_sources_from_chain(self, chain: Iterable[Any]) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        for segment in chain or []:
            class_name = segment.__class__.__name__.lower()
            segment_type = str(getattr(segment, "type", "") or class_name).lower()
            if "image" in segment_type or "image" in class_name:
                source = self._image_source_from_object(segment)
                if source:
                    sources.append(source)
            if "reply" in segment_type or "reply" in class_name:
                reply_id = str(
                    getattr(segment, "id", "")
                    or getattr(segment, "message_id", "")
                    or getattr(segment, "msg_id", "")
                    or ""
                ).strip()
                if reply_id:
                    reply_ids.append(reply_id)
                for attr in ("message", "chain", "message_chain", "content"):
                    nested = getattr(segment, attr, None)
                    if isinstance(nested, list):
                        nested_sources, nested_reply_ids = self._extract_sources_from_chain(nested)
                        sources.extend(nested_sources)
                        reply_ids.extend(nested_reply_ids)
        return sources, reply_ids

    def _extract_references_from_raw(self, raw: Any) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        if raw is None:
            return sources, reply_ids
        if isinstance(raw, str):
            _, cq_reply_ids = self._extract_sources_from_cq_text(raw)
            return sources, cq_reply_ids
        if isinstance(raw, list):
            for item in raw:
                child_sources, child_reply_ids = self._extract_references_from_raw(item)
                sources.extend(child_sources)
                reply_ids.extend(child_reply_ids)
            return sources, reply_ids
        if isinstance(raw, dict):
            raw_type = str(raw.get("type") or raw.get("post_type") or "").lower()
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            if raw_type == "reply":
                reply_id = str(data.get("id") or data.get("message_id") or "").strip()
                if reply_id:
                    reply_ids.append(reply_id)
                for key in ("message", "raw_message", "content", "elements"):
                    value = data.get(key) if isinstance(data, dict) else None
                    if isinstance(value, (list, dict, str)):
                        child_sources, child_reply_ids = self._extract_sources_from_raw(value)
                        sources.extend(child_sources)
                        reply_ids.extend(child_reply_ids)
                return sources, reply_ids
            for key in ("message", "raw_message", "content", "elements"):
                value = raw.get(key)
                if isinstance(value, (list, dict)):
                    child_sources, child_reply_ids = self._extract_references_from_raw(value)
                    sources.extend(child_sources)
                    reply_ids.extend(child_reply_ids)
        return sources, reply_ids

    def _extract_sources_from_raw(self, raw: Any) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        if raw is None:
            return sources, reply_ids
        if isinstance(raw, str):
            return self._extract_sources_from_cq_text(raw)
        if isinstance(raw, list):
            for item in raw:
                child_sources, child_reply_ids = self._extract_sources_from_raw(item)
                sources.extend(child_sources)
                reply_ids.extend(child_reply_ids)
            return sources, reply_ids
        if isinstance(raw, dict):
            raw_type = str(raw.get("type") or raw.get("post_type") or "").lower()
            data = raw.get("data") if isinstance(raw.get("data"), dict) else raw
            if raw_type == "image":
                source = self._image_source_from_mapping(data)
                if source:
                    sources.append(source)
            if raw_type == "reply":
                reply_id = str(data.get("id") or data.get("message_id") or "").strip()
                if reply_id:
                    reply_ids.append(reply_id)
            for key in ("message", "raw_message", "content", "elements"):
                value = raw.get(key)
                if isinstance(value, (list, dict)):
                    child_sources, child_reply_ids = self._extract_sources_from_raw(value)
                    sources.extend(child_sources)
                    reply_ids.extend(child_reply_ids)
        return sources, reply_ids

    def _extract_sources_from_cq_text(self, raw: str) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        for segment_type, payload in re.findall(r"\[CQ:([^,\]]+),([^\]]*)\]", raw):
            attrs = self._parse_cq_attrs(payload)
            if segment_type.lower() == "image":
                source = self._image_source_from_mapping(attrs)
                if source and _is_safe_raw_cq_image_source(source):
                    sources.append(source)
            elif segment_type.lower() == "reply":
                reply_id = str(attrs.get("id") or attrs.get("message_id") or "").strip()
                if reply_id:
                    reply_ids.append(reply_id)
        return sources, reply_ids

    def _parse_cq_attrs(self, payload: str) -> dict[str, str]:
        attrs: dict[str, str] = {}
        for part in payload.split(","):
            if "=" not in part:
                continue
            key, value = part.split("=", 1)
            attrs[key.strip()] = value.strip()
        return attrs

    def _image_source_from_object(self, segment: Any) -> str:
        for attr in ("url", "file", "path", "image_url", "image", "src"):
            value = getattr(segment, attr, None)
            if value:
                return str(value)
        with contextlib.suppress(Exception):
            value = segment.to_dict()
            if isinstance(value, dict):
                return self._image_source_from_mapping(value)
        return ""

    def _image_source_from_mapping(self, mapping: dict[str, Any]) -> str:
        for key in ("url", "file", "path", "image_url", "image", "src"):
            value = mapping.get(key)
            if value:
                return str(value)
        data = mapping.get("data")
        if isinstance(data, dict):
            return self._image_source_from_mapping(data)
        return ""

    async def _fetch_quoted_message(self, event: Any, message_id: str) -> Any:
        bot = getattr(event, "bot", None) or getattr(event, "_bot", None)
        if not bot:
            return None
        call_candidates = [
            getattr(getattr(bot, "api", None), "call_action", None),
            getattr(bot, "call_action", None),
            getattr(getattr(bot, "api", None), "get_msg", None),
            getattr(bot, "get_msg", None),
        ]
        for caller in call_candidates:
            if not callable(caller):
                continue
            with contextlib.suppress(Exception):
                if getattr(caller, "__name__", "") == "get_msg":
                    return await caller(message_id=int(message_id) if message_id.isdigit() else message_id)
                return await caller("get_msg", message_id=int(message_id) if message_id.isdigit() else message_id)
        return None

    async def _prepare_image(self, source: str, *, max_bytes: int, timeout: int) -> PreparedImage | None:
        source = str(source or "").strip()
        if not source:
            return None
        if source.startswith("data:image/"):
            header, payload = source.split(",", 1)
            mime = header.split(";", 1)[0].replace("data:", "").lower()
            data = _decode_limited_base64(payload, max_bytes=max_bytes)
            return self._write_temp_image(data, mime, max_bytes=max_bytes, label="reference")
        if source.startswith("base64://"):
            data = _decode_limited_base64(source.removeprefix("base64://"), max_bytes=max_bytes)
            return self._write_temp_image(data, "image/png", max_bytes=max_bytes, label="reference")
        if source.startswith("http://") or source.startswith("https://"):
            return await self._download_http_image(source, max_bytes=max_bytes, timeout=timeout)
        file_path = Path(source.removeprefix("file://")).expanduser()
        if file_path.exists() and file_path.is_file():
            mime = mimetypes.guess_type(str(file_path))[0] or "image/png"
            if mime.lower() not in IMAGE_MIME_TYPES:
                return None
            if file_path.stat().st_size > max_bytes:
                raise UserFacingError("参考图文件过大。")
            return self._write_temp_image(file_path.read_bytes(), mime, max_bytes=max_bytes, label="reference")
        return None

    def _write_temp_image(self, data: bytes, mime: str, *, max_bytes: int, label: str) -> PreparedImage:
        mime = mime.lower().replace("image/jpg", "image/jpeg")
        if mime not in IMAGE_MIME_TYPES:
            raise UserFacingError("参考图只支持 PNG、JPEG、WEBP。")
        if len(data) > max_bytes:
            raise UserFacingError("参考图文件过大。")
        if not data:
            raise UserFacingError("参考图为空。")
        extension = EXTENSION_BY_MIME.get(mime, "png")
        path = self.temp_files.write_bytes(data, label=label, extension=extension)
        return PreparedImage(path, str(path), mime, temporary=True)

    async def _download_http_image(self, source: str, *, max_bytes: int, timeout: int) -> PreparedImage:
        current_url = source
        await _ensure_public_http_url(current_url)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(6):
                async with client.stream("GET", current_url) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise UserFacingError("参考图 URL 重定向无效。")
                        current_url = urljoin(current_url, location)
                        await _ensure_public_http_url(current_url)
                        continue

                    response.raise_for_status()
                    mime = response.headers.get("content-type", "image/png").split(";", 1)[0].lower()
                    data = await _read_limited_response(response, max_bytes=max_bytes)
                    return self._write_temp_image(data, mime, max_bytes=max_bytes, label="reference")
        raise UserFacingError("参考图 URL 重定向次数过多。")


def _dedupe(values: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _is_safe_raw_cq_image_source(source: str) -> bool:
    normalized = str(source or "").strip().lower()
    return normalized.startswith(("http://", "https://", "data:image/", "base64://"))


def _decode_limited_base64(payload: str, *, max_bytes: int) -> bytes:
    normalized = re.sub(r"\s+", "", str(payload or ""))
    if len(normalized) > ((max_bytes + 2) // 3) * 4 + 4:
        raise UserFacingError("参考图文件过大。")
    data = base64.b64decode(normalized)
    if len(data) > max_bytes:
        raise UserFacingError("参考图文件过大。")
    return data


async def _ensure_public_http_url(url: str) -> None:
    parts = urlsplit(url)
    if parts.scheme not in {"http", "https"} or not parts.hostname:
        raise UserFacingError("参考图 URL 无效。")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as error:
        raise UserFacingError("参考图 URL 端口无效。") from error

    host = parts.hostname.strip().lower()
    if _is_blocked_hostname(host):
        raise UserFacingError("参考图 URL 不允许访问本机或内网地址。")

    literal_ip = _parse_ip_address(host)
    if literal_ip is not None:
        if not literal_ip.is_global:
            raise UserFacingError("参考图 URL 不允许访问本机或内网地址。")
        return

    addresses = await asyncio.to_thread(_resolve_host_addresses, host, port)
    if not addresses:
        raise UserFacingError("参考图 URL 无法解析。")
    if any(not address.is_global for address in addresses):
        raise UserFacingError("参考图 URL 不允许访问本机或内网地址。")


def _is_blocked_hostname(host: str) -> bool:
    normalized = host.strip(".").lower()
    return normalized == "localhost" or normalized.endswith(".localhost") or "%" in normalized


def _parse_ip_address(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    with contextlib.suppress(ValueError):
        return ipaddress.ip_address(host)
    return None


def _resolve_host_addresses(host: str, port: int) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror as error:
        raise UserFacingError("参考图 URL 无法解析。") from error

    addresses: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    seen: set[str] = set()
    for info in infos:
        raw_address = str(info[4][0])
        if raw_address in seen:
            continue
        seen.add(raw_address)
        with contextlib.suppress(ValueError):
            addresses.append(ipaddress.ip_address(raw_address))
    return addresses


async def _read_limited_response(response: httpx.Response, *, max_bytes: int) -> bytes:
    content_length = response.headers.get("content-length")
    if content_length:
        with contextlib.suppress(ValueError):
            content_length_value = int(content_length)
            if content_length_value > max_bytes:
                raise UserFacingError("参考图文件过大。")

    payload = bytearray()
    async for chunk in response.aiter_bytes():
        payload.extend(chunk)
        if len(payload) > max_bytes:
            raise UserFacingError("参考图文件过大。")
    return bytes(payload)

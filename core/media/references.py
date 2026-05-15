from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import ipaddress
import mimetypes
import re
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urljoin, urlsplit

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
        raw_message = getattr(message_obj, "raw_message", None) if message_obj else None
        sources: list[str] = []
        reply_ids: list[str] = []
        for chain in self._message_chains_from_event(event, message_obj):
            chain_sources, chain_reply_ids = self._extract_references_from_chain(chain)
            sources.extend(chain_sources)
            reply_ids.extend(chain_reply_ids)

        raw_sources, raw_reply_ids = self._extract_references_from_raw(raw_message)
        sources.extend(raw_sources)
        reply_ids.extend(raw_reply_ids)

        for reply_id in _dedupe(reply_ids):
            quoted = await self._fetch_quoted_message(event, reply_id)
            quoted_sources, nested_reply_ids = await self._extract_sources_from_quoted_payload(event, quoted)
            sources.extend(quoted_sources)
            for nested_reply_id in nested_reply_ids:
                nested = await self._fetch_quoted_message(event, nested_reply_id)
                nested_sources, _ = await self._extract_sources_from_quoted_payload(event, nested)
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
            nested_sources, nested_reply_ids = self._extract_sources_from_chain(
                self._component_children(segment)
            )
            sources.extend(nested_sources)
            reply_ids.extend(nested_reply_ids)
        return sources, reply_ids

    def _extract_sources_from_chain(self, chain: Iterable[Any]) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        for segment in self._walk_components(chain or []):
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
                if isinstance(value, (list, dict, str)):
                    child_sources, child_reply_ids = self._extract_references_from_raw(value)
                    sources.extend(child_sources)
                    reply_ids.extend(child_reply_ids)
            data = raw.get("data")
            if isinstance(data, dict) and data is not raw:
                child_sources, child_reply_ids = self._extract_references_from_raw(data)
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
                if isinstance(value, (list, dict, str)):
                    child_sources, child_reply_ids = self._extract_sources_from_raw(value)
                    sources.extend(child_sources)
                    reply_ids.extend(child_reply_ids)
            nested_data = raw.get("data")
            if isinstance(nested_data, dict) and nested_data is not raw:
                child_sources, child_reply_ids = self._extract_sources_from_raw(nested_data)
                sources.extend(child_sources)
                reply_ids.extend(child_reply_ids)
        return sources, reply_ids

    def _extract_sources_from_cq_text(self, raw: str) -> tuple[list[str], list[str]]:
        sources: list[str] = []
        reply_ids: list[str] = []
        for segment in self._raw_cq_segments(raw):
            segment_type = str(segment.get("type") or "").lower()
            attrs = segment.get("data") if isinstance(segment.get("data"), dict) else {}
            if segment_type.lower() == "image":
                source = self._image_source_from_mapping(attrs, allow_local=False)
                if source:
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
            key = html.unescape(key).strip()
            if key:
                attrs[key] = html.unescape(value).strip()
        return attrs

    def _image_source_from_object(self, segment: Any) -> str:
        data = getattr(segment, "data", None)
        source = self._image_source_from_mapping(data, allow_local=True) if isinstance(data, dict) else ""
        if source:
            return source
        for attr in ("url", "file", "path", "image_url", "image", "src"):
            value = getattr(segment, attr, None)
            source = self._source_from_value(value, allow_local=True)
            if source:
                return source
        with contextlib.suppress(Exception):
            value = segment.to_dict()
            if isinstance(value, dict):
                return self._image_source_from_mapping(value, allow_local=True)
        return ""

    def _image_source_from_mapping(self, mapping: dict[str, Any], *, allow_local: bool = False) -> str:
        if not isinstance(mapping, dict):
            return ""
        for key in ("url", "file", "path", "image_url", "image", "src"):
            source = self._source_from_value(mapping.get(key), allow_local=allow_local)
            if source:
                return source
        data = mapping.get("data")
        if isinstance(data, dict):
            return self._image_source_from_mapping(data, allow_local=allow_local)
        return ""

    def _source_from_value(self, value: Any, *, allow_local: bool = False) -> str:
        if isinstance(value, str):
            source = _strip_media_prefixes(value.strip())
            return source if _is_usable_image_source(source, allow_local=allow_local) else ""
        if isinstance(value, dict):
            return self._image_source_from_mapping(value, allow_local=allow_local)
        return ""

    async def _fetch_quoted_message(self, event: Any, message_id: str) -> Any:
        return await self._call_platform_action_by_message_id(event, "get_msg", message_id)

    async def _extract_sources_from_quoted_payload(
        self,
        event: Any,
        payload: Any,
    ) -> tuple[list[str], list[str]]:
        sources, reply_ids = self._extract_sources_from_raw(payload)
        references = self._image_references_from_onebot_payload(payload)
        sources.extend(self._sources_from_image_references(references))
        sources.extend(await self._resolve_remote_file_sources(event, references))
        return _dedupe(sources), _dedupe(reply_ids)

    def _message_chains_from_event(self, event: Any, message_obj: Any) -> list[list[Any]]:
        chains: list[list[Any]] = []
        seen: set[int] = set()
        for chain in (self._safe_get_messages(event), getattr(message_obj, "message", []) if message_obj else []):
            if not isinstance(chain, list):
                continue
            marker = id(chain)
            if marker in seen:
                continue
            seen.add(marker)
            chains.append(chain)
        return chains

    def _safe_get_messages(self, event: Any) -> list[Any]:
        getter = getattr(event, "get_messages", None)
        if not callable(getter):
            return []
        with contextlib.suppress(Exception):
            return list(getter() or [])
        return []

    def _component_children(self, component: Any) -> list[Any]:
        children: list[Any] = []
        for attr in ("message", "chain", "message_chain", "content", "nodes", "elements"):
            nested = getattr(component, attr, None)
            if isinstance(nested, list):
                children.extend(nested)
        data = getattr(component, "data", None)
        if isinstance(data, dict):
            for key in ("message", "chain", "message_chain", "content", "nodes", "elements"):
                nested = data.get(key)
                if isinstance(nested, list):
                    children.extend(nested)
        return children

    def _walk_components(self, parts: Iterable[Any], seen: set[int] | None = None) -> Iterable[Any]:
        seen = seen if seen is not None else set()
        for component in parts:
            marker = id(component)
            if marker in seen:
                continue
            seen.add(marker)
            yield component
            children = self._component_children(component)
            if children:
                yield from self._walk_components(children, seen)

    async def _call_platform_action_by_message_id(self, event: Any, action: str, message_id: str) -> Any:
        params_list: list[dict[str, Any]] = [
            {"message_id": message_id},
            {"id": message_id},
        ]
        if message_id.isdigit():
            int_id = int(message_id)
            params_list.extend([{"message_id": int_id}, {"id": int_id}])
        return await self._call_platform_action_variants(event, action, params_list)

    async def _call_platform_action_variants(
        self,
        event: Any,
        action: str,
        params_list: Iterable[dict[str, Any]],
    ) -> Any:
        call_action = self._resolve_call_action(event)
        if not call_action:
            return None
        for params in params_list:
            with contextlib.suppress(Exception):
                result = call_action(action, **params)
                if asyncio.iscoroutine(result):
                    result = await result
                payload = _unwrap_action_result(result)
                if payload is not None:
                    return payload
        return None

    def _resolve_call_action(self, event: Any) -> Any:
        bot = getattr(event, "bot", None)
        api = getattr(bot, "api", None)
        call_action = getattr(api, "call_action", None)
        if callable(call_action):
            return call_action
        call_action = getattr(bot, "call_action", None)
        return call_action if callable(call_action) else None

    def _image_references_from_onebot_payload(self, payload: Any) -> list[dict[str, str]]:
        payloads = _payload_layers(payload)
        references: list[dict[str, str]] = []
        for current in payloads:
            for segment in self._walk_onebot_segments(self._onebot_payload_segments(current)):
                if isinstance(segment, dict):
                    self._append_image_reference(
                        references,
                        self._image_reference_from_onebot_segment(segment),
                    )
            raw_message = str(current.get("raw_message", "") or "")
            for segment in self._raw_cq_segments(raw_message):
                self._append_image_reference(
                    references,
                    self._image_reference_from_onebot_segment(segment),
                )
        return references

    def _onebot_payload_segments(self, payload: dict[str, Any]) -> list[Any]:
        segments = payload.get("message") or payload.get("messages") or payload.get("elements")
        return segments if isinstance(segments, list) else []

    def _walk_onebot_segments(self, segments: Iterable[Any]) -> Iterable[Any]:
        for segment in segments:
            yield segment
            if not isinstance(segment, dict):
                continue
            data = segment.get("data")
            if isinstance(data, dict):
                for key in ("content", "message", "messages", "nodes", "elements"):
                    nested = data.get(key)
                    if isinstance(nested, list):
                        yield from self._walk_onebot_segments(nested)

    def _image_reference_from_onebot_segment(self, segment: dict[str, Any]) -> dict[str, str]:
        segment_type = str(segment.get("type", "") or "").lower()
        if segment_type not in {"image", "file"}:
            return {}
        data = segment.get("data")
        if not isinstance(data, dict):
            return {}
        source = self._image_source_from_mapping(data, allow_local=False)
        file_id = _first_mapping_text(data, ("file_id", "fileid", "id"))
        file_name = _first_mapping_text(data, ("file_name", "name", "file", "path", "file_path", "url"))
        is_image = segment_type == "image" or _is_image_filename(file_name)
        if not is_image and source:
            is_image = _is_image_filename(source)
        if not is_image:
            return {}
        return {
            "source": source,
            "file_id": file_id,
            "file_name": file_name,
            "segment_type": segment_type,
        }

    def _append_image_reference(self, references: list[dict[str, str]], reference: dict[str, str]) -> None:
        if not reference:
            return
        if not reference.get("source") and not reference.get("file_id"):
            return
        key = (
            reference.get("source", ""),
            reference.get("file_id", ""),
            reference.get("file_name", ""),
            reference.get("segment_type", ""),
        )
        for existing in references:
            existing_key = (
                existing.get("source", ""),
                existing.get("file_id", ""),
                existing.get("file_name", ""),
                existing.get("segment_type", ""),
            )
            if existing_key == key:
                return
        references.append(reference)

    def _sources_from_image_references(self, references: Iterable[dict[str, str]]) -> list[str]:
        sources: list[str] = []
        for reference in references:
            source = str(reference.get("source", "") or "").strip()
            if source and source not in sources:
                sources.append(source)
        return sources

    async def _resolve_remote_file_sources(
        self,
        event: Any,
        references: Iterable[dict[str, str]],
    ) -> list[str]:
        sources: list[str] = []
        for reference in references:
            if reference.get("source"):
                continue
            file_id = str(reference.get("file_id", "") or "").strip()
            if not file_id:
                continue
            for source in await self._resolve_one_remote_file_source(event, file_id):
                if source not in sources:
                    sources.append(source)
        return sources

    async def _resolve_one_remote_file_source(self, event: Any, file_id: str) -> list[str]:
        payload = await self._call_platform_action_variants(
            event,
            "get_file",
            [{"file_id": file_id}],
        )
        source = self._source_from_action_payload(payload)
        if source:
            return [source]

        if self._event_is_private_chat(event):
            payload = await self._call_platform_action_variants(
                event,
                "get_private_file_url",
                [{"file_id": file_id}],
            )
            source = self._source_from_action_payload(payload)
            return [source] if source else []

        group_id = self._event_group_id(event)
        if not group_id:
            return []
        params_list: list[dict[str, Any]] = [{"group_id": group_id, "file_id": file_id}]
        if str(group_id).isdigit():
            params_list.append({"group_id": int(group_id), "file_id": file_id})
        payload = await self._call_platform_action_variants(event, "get_group_file_url", params_list)
        source = self._source_from_action_payload(payload)
        return [source] if source else []

    def _source_from_action_payload(self, payload: Any) -> str:
        source = self._source_from_value(payload, allow_local=False)
        if source:
            return source
        for current in _payload_layers(payload):
            source = self._image_source_from_mapping(current, allow_local=False)
            if source:
                return source
        return ""

    def _event_is_private_chat(self, event: Any) -> bool:
        is_private_chat = getattr(event, "is_private_chat", None)
        if not callable(is_private_chat):
            return False
        with contextlib.suppress(Exception):
            return bool(is_private_chat())
        return False

    def _event_group_id(self, event: Any) -> str:
        get_group_id = getattr(event, "get_group_id", None)
        if not callable(get_group_id):
            return ""
        with contextlib.suppress(Exception):
            return str(get_group_id() or "").strip()
        return ""

    def _raw_cq_segments(self, raw_message: str) -> list[dict[str, Any]]:
        if not raw_message:
            return []
        segments: list[dict[str, Any]] = []
        for match in re.finditer(r"\[CQ:([^,\]]+)(?:,([^\]]*))?\]", raw_message):
            segment_type = html.unescape(match.group(1)).strip()
            data = self._parse_cq_attrs(match.group(2) or "")
            segments.append({"type": segment_type, "data": data})
        return segments

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
        file_path = _local_file_path_from_source(source)
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


def _strip_media_prefixes(value: str) -> str:
    text = value.strip()
    for prefix in ("range:", "proxy:", "cache:"):
        if text.startswith(prefix):
            text = text[len(prefix):]
    return text


def _is_usable_image_source(source: str, *, allow_local: bool = False) -> bool:
    normalized = str(source or "").strip()
    if not normalized:
        return False
    if normalized.lower().startswith(("http://", "https://", "data:image/", "base64://")):
        return True
    if not allow_local:
        return False
    file_path = _local_file_path_from_source(normalized)
    if not file_path.exists() or not file_path.is_file():
        return False
    mime = mimetypes.guess_type(str(file_path))[0] or "image/png"
    return mime.lower() in IMAGE_MIME_TYPES


def _local_file_path_from_source(source: str) -> Path:
    text = str(source or "").strip()
    if text.lower().startswith("file://"):
        parts = urlsplit(text)
        path = unquote(parts.path or "")
        if parts.netloc:
            if re.fullmatch(r"[A-Za-z]:", parts.netloc):
                path = f"{parts.netloc}{path}"
            else:
                path = f"//{parts.netloc}{path}"
        if re.match(r"^/[A-Za-z]:[\\/]", path):
            path = path[1:]
        return Path(path).expanduser()
    return Path(text).expanduser()


def _is_image_filename(value: str) -> bool:
    text = str(value or "").strip()
    if not text:
        return False
    if text.lower().startswith(("http://", "https://", "file://")):
        text = urlsplit(text).path
    return Path(text).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp"}


def _first_mapping_text(value: dict[str, Any], keys: Iterable[str]) -> str:
    for key in keys:
        item = value.get(key)
        if item is None:
            continue
        text = str(item).strip()
        if text:
            return text
    return ""


def _payload_layers(payload: Any) -> list[dict[str, Any]]:
    layers: list[dict[str, Any]] = []
    current = payload
    seen: set[int] = set()
    while isinstance(current, dict) and id(current) not in seen:
        seen.add(id(current))
        layers.append(current)
        data = current.get("data")
        current = data if isinstance(data, dict) else None
    return layers


def _unwrap_action_result(result: Any) -> Any:
    if isinstance(result, dict):
        data = result.get("data")
        if data is not None:
            return data
    return result


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

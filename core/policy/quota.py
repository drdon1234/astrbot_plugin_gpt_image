from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from ..config import bool_value, int_value


class QuotaExceededError(Exception):
    """Raised when a request would exceed the active quota window."""

    def __init__(
        self,
        *,
        scope: str,
        key: str,
        requested: int,
        used: int,
        active: int,
        limit: int,
        window_minutes: int,
        retry_after_seconds: int,
    ) -> None:
        super().__init__("quota exceeded")
        self.scope = scope
        self.key = key
        self.requested = requested
        self.used = used
        self.active = active
        self.limit = limit
        self.window_minutes = window_minutes
        self.retry_after_seconds = retry_after_seconds

    def user_message(self) -> str:
        """Build a user-facing quota denial message."""
        retry_minutes = max(1, (self.retry_after_seconds + 59) // 60)
        return (
            f"当前{self._scope_label()} {self.window_minutes} 分钟内最多生成 {self.limit} 张图，"
            f"已使用 {self.used} 张，进行中 {self.active} 张，本次需要 {self.requested} 张。"
            f"请约 {retry_minutes} 分钟后再试。"
        )

    def _scope_label(self) -> str:
        return "私聊" if self.scope == "private" else "群聊"


class QuotaLedgerError(Exception):
    """Raised when the persisted quota ledger cannot be read safely."""

    pass


@dataclass(frozen=True)
class QuotaLimit:
    """Configured quota window and maximum image count."""

    enabled: bool
    window_minutes: int
    max_images: int

    @classmethod
    def from_config(cls, config: Mapping[str, Any]) -> "QuotaLimit":
        """Create a bounded quota limit from plugin configuration."""
        return cls(
            enabled=bool_value(config.get("enabled"), True),
            window_minutes=int_value(config.get("window_minutes"), 60, 1, 60 * 24 * 30),
            max_images=int_value(config.get("max_images"), 0, 0, 100000),
        )

    @property
    def window_seconds(self) -> int:
        """Return the quota window length in seconds."""
        return self.window_minutes * 60

    @property
    def is_limited(self) -> bool:
        """Return whether this quota limit actively restricts requests."""
        return self.enabled and self.max_images > 0


@dataclass(frozen=True)
class QuotaSnapshot:
    """Read-only quota usage view for a single scope and key."""

    scope: str
    key: str
    used: int
    active: int
    limit: int
    window_minutes: int
    remaining: int | None


class QuotaReservation:
    """Reservation handle that releases active quota after a request finishes."""

    def __init__(
        self,
        ledger: "QuotaLedger | None",
        reservation_id: str = "",
        cost: int = 0,
        limited: bool = False,
    ) -> None:
        self._ledger = ledger
        self.reservation_id = reservation_id
        self.cost = cost
        self.limited = limited
        self._released = False

    async def release(self, success_count: int = 0) -> None:
        """Release reserved quota and record successful image count once."""
        if self._released:
            return
        if self._ledger and self.reservation_id:
            await self._ledger.release(self.reservation_id, success_count=success_count)
        self._released = True


class QuotaLedger:
    """JSON-backed quota ledger with in-process active request reservations."""

    def __init__(self, path: Path, *, now: Callable[[], float] | None = None) -> None:
        self.path = Path(path)
        self.now = now or time.time
        self._lock = asyncio.Lock()
        self._active: dict[str, dict[str, Any]] = {}

    async def reserve(
        self,
        *,
        scope: str,
        key: str,
        cost: int,
        limit: QuotaLimit,
        metadata: Mapping[str, Any] | None = None,
    ) -> QuotaReservation:
        """Reserve quota before generation so concurrent requests count correctly."""
        cost = max(1, int(cost))
        if not limit.is_limited:
            return QuotaReservation(None, cost=cost, limited=False)

        async with self._lock:
            data = await self._read_data()
            now = float(self.now())
            since = now - limit.window_seconds
            self._prune(data, now, limit)
            used = self._count_events(data, scope, key, since)
            active = self._count_active(scope, key, since)
            if used + active + cost > limit.max_images:
                raise QuotaExceededError(
                    scope=scope,
                    key=key,
                    requested=cost,
                    used=used,
                    active=active,
                    limit=limit.max_images,
                    window_minutes=limit.window_minutes,
                    retry_after_seconds=self._retry_after_seconds(data, scope, key, since),
                )

            reservation_id = uuid.uuid4().hex
            entry = {
                "scope": scope,
                "key": key,
                "cost": cost,
                "started_at": now,
                "metadata": dict(metadata or {}),
            }
            await self._write_data(data)
            self._active[reservation_id] = entry
            return QuotaReservation(self, reservation_id, cost=cost, limited=True)

    async def release(self, reservation_id: str, *, success_count: int = 0) -> None:
        """Release an active reservation and persist the successful image count."""
        async with self._lock:
            entry = self._active.get(reservation_id)
            if not entry:
                return
            success_count = max(0, min(int(success_count or 0), int(entry.get("cost") or 0)))
            if success_count <= 0:
                self._active.pop(reservation_id, None)
                return
            data = await self._read_data()
            event = {
                "scope": entry["scope"],
                "key": entry["key"],
                "count": success_count,
                "created_at": float(self.now()),
                "metadata": entry.get("metadata") or {},
            }
            data.setdefault("events", []).append(event)
            await self._write_data(data)
            self._active.pop(reservation_id, None)

    async def snapshot(self, *, scope: str, key: str, limit: QuotaLimit) -> QuotaSnapshot:
        """Read used, active, and remaining quota for one scope and key."""
        if not limit.is_limited:
            return QuotaSnapshot(scope, key, used=0, active=0, limit=0, window_minutes=limit.window_minutes, remaining=None)
        async with self._lock:
            data = await self._read_data()
            now = float(self.now())
            since = now - limit.window_seconds
            used = self._count_events(data, scope, key, since)
            active = self._count_active(scope, key, since)
            remaining = max(0, limit.max_images - used - active)
            return QuotaSnapshot(scope, key, used, active, limit.max_images, limit.window_minutes, remaining)

    async def _read_data(self) -> dict[str, Any]:
        """Read and validate the quota JSON ledger from disk."""
        if not self.path.exists():
            return {"version": 1, "events": []}
        try:
            payload = await asyncio.to_thread(self.path.read_text, encoding="utf-8")
        except FileNotFoundError:
            return {"version": 1, "events": []}
        except OSError as error:
            raise QuotaLedgerError("生图配额账本读取失败。") from error

        try:
            data = json.loads(payload)
        except json.JSONDecodeError as error:
            await asyncio.to_thread(self._backup_corrupt_data)
            raise QuotaLedgerError("生图配额账本损坏，请检查 usage.json。") from error
        if not isinstance(data, dict) or not isinstance(data.get("events", []), list):
            await asyncio.to_thread(self._backup_corrupt_data)
            raise QuotaLedgerError("生图配额账本结构无效，请检查 usage.json。")
        return data

    async def _write_data(self, data: Mapping[str, Any]) -> None:
        await asyncio.to_thread(self.path.parent.mkdir, parents=True, exist_ok=True)
        payload = json.dumps(data, ensure_ascii=False, indent=2)
        await asyncio.to_thread(self._write_data_sync, payload)

    def _write_data_sync(self, payload: str) -> None:
        temp_path = self.path.with_name(f"{self.path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            os.replace(temp_path, self.path)
        finally:
            with contextlib.suppress(OSError):
                temp_path.unlink()

    def _backup_corrupt_data(self) -> None:
        if not self.path.exists():
            return
        backup_path = self.path.with_name(f"{self.path.name}.corrupt-{int(self.now())}")
        with contextlib.suppress(OSError):
            if not backup_path.exists():
                shutil.copy2(self.path, backup_path)

    def _count_events(self, data: Mapping[str, Any], scope: str, key: str, since: float) -> int:
        return sum(
            int(event.get("count") or 0)
            for event in data.get("events", [])
            if event.get("scope") == scope and event.get("key") == key and float(event.get("created_at") or 0) >= since
        )

    def _count_active(self, scope: str, key: str, since: float) -> int:
        return sum(
            int(entry.get("cost") or 0)
            for entry in self._active.values()
            if entry.get("scope") == scope and entry.get("key") == key and float(entry.get("started_at") or 0) >= since
        )

    def _retry_after_seconds(self, data: Mapping[str, Any], scope: str, key: str, since: float) -> int:
        matching = [
            float(event.get("created_at") or 0)
            for event in data.get("events", [])
            if event.get("scope") == scope and event.get("key") == key and float(event.get("created_at") or 0) >= since
        ]
        if not matching:
            return 60
        oldest = min(matching)
        return max(60, int(oldest - since) + 1)

    def _prune(self, data: dict[str, Any], now: float, limit: QuotaLimit) -> None:
        retention_seconds = max(7 * 24 * 60 * 60, limit.window_seconds * 2)
        cutoff = now - retention_seconds
        data["events"] = [
            event
            for event in data.get("events", [])
            if float(event.get("created_at") or 0) >= cutoff
        ]

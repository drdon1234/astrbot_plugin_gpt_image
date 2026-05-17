from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
from pathlib import Path


class TempFileManager:
    """Own and safely clean up temporary files created by the plugin."""

    def __init__(self, plugin_name: str) -> None:
        self.root = Path(tempfile.mkdtemp(prefix=f"{plugin_name}-"))
        self._closed = False
        self._owned: set[Path] = set()

    def write_bytes(self, data: bytes, *, label: str, extension: str) -> Path:
        """Write bytes into the plugin temp root and mark the path as owned."""
        if self._closed:
            raise RuntimeError("temporary file manager is closed")
        safe_label = _safe_token(label or "image")
        safe_extension = _safe_token(extension or "bin")
        fd, raw_path = tempfile.mkstemp(prefix=f"{safe_label}-", suffix=f".{safe_extension}", dir=self.root)
        os.close(fd)
        path = Path(raw_path)
        self._owned.add(path.resolve())
        try:
            path.write_bytes(data)
        except Exception:
            self.remove_file(path)
            raise
        return path

    async def cleanup_file(self, path: str | Path) -> None:
        """Asynchronously remove one plugin-owned temporary file."""
        await asyncio.to_thread(self.remove_file, path)

    def remove_file(self, path: str | Path) -> None:
        """Remove a single file only when it is known to belong to this manager."""
        target = Path(path)
        if not self._is_owned_path(target):
            return
        try:
            target.unlink(missing_ok=True)
        except OSError:
            return
        self._owned.discard(target.resolve())

    async def cleanup_all(self) -> None:
        """Remove the entire plugin temp root and close the manager."""
        self._closed = True
        self._owned.clear()
        await asyncio.to_thread(shutil.rmtree, self.root, ignore_errors=True)

    def _is_owned_path(self, path: Path) -> bool:
        try:
            target = path.resolve()
            root = self.root.resolve()
        except OSError:
            return False
        return target in self._owned or target == root or root in target.parents


def _safe_token(value: str) -> str:
    token = "".join(char for char in value.lower() if char.isalnum() or char in {"-", "_"})
    return token.strip("-_") or "tmp"

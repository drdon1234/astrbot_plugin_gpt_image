from __future__ import annotations

from pathlib import Path
from typing import Callable


def resolve_plugin_data_dir(plugin_name: str, data_path_getter: Callable[[], str]) -> Path:
    base = Path(data_path_getter())
    data_dir = base / "plugin_data" / plugin_name
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

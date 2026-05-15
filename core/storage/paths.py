from __future__ import annotations

from pathlib import Path
from typing import Callable


def resolve_plugin_data_dir(plugin_name: str, data_path_getter: Callable[[], str] | None = None) -> Path:
    if callable(data_path_getter):
        base = Path(data_path_getter())
    else:
        base = Path("data")

    data_dir = base / "plugin_data" / plugin_name
    data_dir.mkdir(parents=True, exist_ok=True)
    return data_dir

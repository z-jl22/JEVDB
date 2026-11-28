"""Persist editable service defaults alongside the workbench's runtime data."""

from __future__ import annotations

import json
import os
from pathlib import Path
from tempfile import NamedTemporaryFile
from threading import RLock
from typing import Callable

SettingsValues = dict[str, str | int | float]

EDITABLE_FIELDS = (
    "deepseek_base_url", "deepseek_model", "deepseek_api_key", "finnhub_api_key",
    "sec_user_agent", "llm_batch_k", "llm_threads", "llm_timeout",
)
_lock = RLock()


def read_overrides(runtime_dir: Path) -> SettingsValues:
    try:
        with (runtime_dir / "web-settings.json").open(encoding="utf-8") as file:
            data = json.load(file)
    except FileNotFoundError:
        return {}
    if not isinstance(data, dict):
        raise ValueError("网页设置文件格式无效。")
    return {key: data[key] for key in EDITABLE_FIELDS if key in data}


def update_overrides(
    runtime_dir: Path,
    change: Callable[[SettingsValues], SettingsValues],
) -> SettingsValues:
    with _lock:
        overrides = change(read_overrides(runtime_dir))
        runtime_dir.mkdir(parents=True, exist_ok=True)
        destination = runtime_dir / "web-settings.json"
        # NamedTemporaryFile creates a private file on POSIX. Replace atomically for readers.
        temporary = None
        try:
            with NamedTemporaryFile(
                dir=runtime_dir, prefix=".web-settings-", delete=False, mode="w", encoding="utf-8"
            ) as file:
                temporary = Path(file.name)
                json.dump(overrides, file, ensure_ascii=False, indent=2)
                file.write("\n")
                file.flush()
                os.fsync(file.fileno())
            temporary.replace(destination)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
        return overrides

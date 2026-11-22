from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any
import asyncio


class OptimizerError(RuntimeError):
    """An actionable configuration, input, or network failure."""


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def write_json(path: Path, value: Any) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n")


def resolve_path(base: Path, path: str) -> Path:
    return (base / path).resolve()


def read_tokens(path: Path) -> list[str]:
    """Read UTF-8 token lines; missing files, comments and examples are empty."""
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except FileNotFoundError:
        return []
    except (OSError, UnicodeError) as exc:
        raise OptimizerError(f"无法读取 Token 文件 {path} ({type(exc).__name__})") from exc
    return list(
        dict.fromkeys(
            line.strip() for line in lines if line.strip() and not line.lstrip().startswith(("#", "YOUR_"))
        )
    )


async def bounded_map(function, values, concurrency: int) -> list:
    """Use a bounded worker pool rather than a task for every candidate."""
    values = list(values)
    result = [None] * len(values)
    iterator = iter(enumerate(values))

    async def worker():
        for index, value in iterator:
            result[index] = await function(value)

    tasks = [asyncio.create_task(worker()) for _ in range(min(int(concurrency), len(values)))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
    return result

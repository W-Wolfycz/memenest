"""库里 rel_path 的路径守卫：只允许落在插件数据目录内。

写入侧（ingest/pipeline.py）与读取侧（panel.py / main.py）共用这一份实现，
避免「写的时候校验、读的时候忘了校验」。
"""

from __future__ import annotations

from pathlib import Path


def is_safe_rel_path(rel_path: str) -> bool:
    """只接受数据目录内的相对正斜杠路径。"""
    text = (rel_path or "").strip()
    if not text or text.startswith("/") or "\\" in text:
        return False
    parts = text.split("/")
    if ".." in parts:
        return False
    if ":" in parts[0]:
        return False
    return True


def resolve_within(root: Path, rel_path: str) -> Path | None:
    """把 rel_path 解析成 root 下的绝对路径；越界或非法时返回 None。"""
    if not is_safe_rel_path(rel_path):
        return None
    try:
        base = Path(root).resolve(strict=False)
        target = (base / rel_path).resolve(strict=False)
        target.relative_to(base)
    except (OSError, ValueError):
        return None
    return target


__all__ = ["is_safe_rel_path", "resolve_within"]

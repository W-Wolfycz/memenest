"""完整性体检：库与磁盘对账。

存在的理由（实测）：一次清理 284 条无描述条目时，其中 25 个文件已经不在磁盘上；
只靠「面板上点开看有没有图」发现不了这类漂移。体检把三类问题摆出来：
库里有、磁盘没有（缺文件）；磁盘有、库里没有（孤儿，只占空间）；各目录实际占用。
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any

CONTENT_DIRS = ("blobs", "inbox")
SCRATCH_DIRS = ("cache", "imports", "tmp")
"""可再生的目录：不进备份，体检里单独报占用。"""

SAMPLE_LIMIT = 20


def _dir_usage(root: Path) -> dict[str, int]:
    if not root.is_dir():
        return {"files": 0, "bytes": 0}
    files = 0
    total = 0
    for path in root.rglob("*"):
        try:
            if path.is_file():
                files += 1
                total += path.stat().st_size
        except OSError:
            # 边扫边被删/无权限：跳过这一项，计数按已扫到的算
            continue
    return {"files": files, "bytes": total}


def scan(data_dir: Path, store: Any) -> dict[str, Any]:
    """对账并返回体检报告（不修改任何数据）。"""
    data_dir = Path(data_dir)
    rows = store.rel_paths()
    known = {rel.replace("\\", "/"): memo_id for memo_id, rel in rows}

    missing: list[dict[str, Any]] = []
    for rel, memo_id in known.items():
        if not (data_dir / rel).is_file():
            missing.append({"id": memo_id, "rel_path": rel})

    orphans: list[str] = []
    for sub in CONTENT_DIRS:
        root = data_dir / sub
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(data_dir).as_posix()
            if rel not in known:
                orphans.append(rel)

    dirs: dict[str, dict[str, int]] = {}
    for sub in CONTENT_DIRS + SCRATCH_DIRS:
        dirs[sub] = _dir_usage(data_dir / sub)

    db_path = data_dir / "memo.db"
    db_sizes = {"db": 0, "wal": 0}
    for suffix, key in (("", "db"), ("-wal", "wal")):
        target = Path(str(db_path) + suffix)
        try:
            db_sizes[key] = target.stat().st_size
        except OSError:
            db_sizes[key] = 0

    return {
        "rows": len(rows),
        "missing_count": len(missing),
        "missing_sample": missing[:SAMPLE_LIMIT],
        "orphan_count": len(orphans),
        "orphan_sample": sorted(orphans)[:SAMPLE_LIMIT],
        "dirs": dirs,
        "db": db_sizes,
        "total_bytes": sum(item["bytes"] for item in dirs.values()) + sum(db_sizes.values()),
    }


def vacuum(store: Any) -> dict[str, int]:
    """回收删除留下的空页；返回前后的库文件大小。"""
    db_path = Path(store.db_path)

    def _size() -> int:
        try:
            return db_path.stat().st_size
        except OSError:
            return 0

    before = _size()
    conn: sqlite3.Connection = store._connect()  # noqa: SLF001 - 同包内的维护入口
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("VACUUM")
    finally:
        conn.close()
    after = _size()
    return {"before": before, "after": after, "saved": max(0, before - after)}


def _orphan_paths(data_dir: Path, store: Any) -> list[str]:
    known = {rel.replace("\\", "/") for _id, rel in store.rel_paths()}
    found: list[str] = []
    for sub in CONTENT_DIRS:
        root = data_dir / sub
        if not root.is_dir():
            continue
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            rel = path.relative_to(data_dir).as_posix()
            if rel not in known:
                found.append(rel)
    return sorted(found)


def remove_missing(data_dir: Path, store: Any) -> dict[str, int]:
    """删掉「库里有记录、磁盘上没有文件」的条目（行删掉，没有文件可删）。

    这类条目点开是空白、发送时必然失败，留着只是污染列表；删除前重新对账一次，
    不信任客户端传来的 id 列表——删除是不可逆的，宁可多查一遍。
    """
    data_dir = Path(data_dir)
    missing = [
        memo_id
        for memo_id, rel in store.rel_paths()
        if not (data_dir / rel).is_file()
    ]
    removed = 0
    for memo_id in missing:
        if store.delete(memo_id) is not None:
            removed += 1
    return {"scanned": len(missing), "removed": removed}


def remove_orphans(
    data_dir: Path, store: Any, rel_paths: list[str] | None = None
) -> dict[str, int]:
    """删除孤儿文件（只允许落在 blobs/inbox 里的路径）。

    rel_paths=None 表示「按当前对账结果全删」——面板上的「清理孤儿」就是这条路径。
    """
    data_dir = Path(data_dir).resolve()
    targets = rel_paths if rel_paths is not None else _orphan_paths(data_dir, store)
    removed = 0
    failed = 0
    for rel in targets:
        target = (data_dir / rel).resolve()
        try:
            target.relative_to(data_dir)
        except ValueError:
            failed += 1
            continue
        if not target.is_file():
            continue
        try:
            os.remove(target)
            removed += 1
        except OSError:
            failed += 1
    return {"scanned": len(targets), "removed": removed, "failed": failed}


__all__ = [
    "CONTENT_DIRS",
    "SCRATCH_DIRS",
    "remove_missing",
    "remove_orphans",
    "scan",
    "vacuum",
]

"""导出备份：把数据库快照与图片文件打包成 zip，放在数据目录的 exports/ 下。

刻意不做 HTTP 大文件下载：受限 iframe 里取几百 MB 的 blob 会被 CORS/内存卡死，
zip 生成在部署端磁盘上、面板告知路径，由用户自行取走——单机部署下这是最短路径。
"""

from __future__ import annotations

import logging
import os
import re
import sqlite3
import time
import zipfile
from datetime import datetime
from io import BytesIO
from pathlib import Path

logger = logging.getLogger(__name__)

EXPORTS_SUBDIR = "exports"
EXPORT_KEEP = 3
"""保留最近几份备份；更旧的在生成新备份时自动清理。"""
_EXPORT_NAME_RE = re.compile(r"^memenest-backup-\d{8}-\d{6}\.zip$")
# 打包哪些子目录：图片的两种状态目录。tmp/imports/压缩缓存都是可再生的，不进备份。
_CONTENT_DIRS = ("blobs", "inbox")

SELECTION_MAX_BYTES = 32 * 1024 * 1024
"""选中导出（只含图片）的 zip 上限：超过就让用户分批，别把面板请求撑爆。"""


class SelectionTooLarge(Exception):
    """选中的图片打包后超过上限。"""


def selection_name(now: datetime) -> str:
    """`2001-02-03T04_05_06_007.zip`：毫秒补齐三位，冒号会被文件系统拒绝，统一换成下划线。"""
    return f"{now:%Y-%m-%dT%H_%M_%S}_{now.microsecond // 1000:03d}.zip"


def build_selection_zip(data_dir: Path, rel_paths: list[str]) -> dict:
    """把选中的图片打成一个 zip（内存里完成，不落盘、不留垃圾）。

    只收图片文件本身，保持原文件名（文件名里已带时间与内容哈希，天然不重名）。
    路径越界或文件已丢失的条目计入 skipped——导出不该因为一条坏记录整体失败。
    """
    root = Path(data_dir).resolve()
    buffer = BytesIO()
    included = 0
    skipped = 0
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as bundle:
        for rel in rel_paths:
            target = (root / rel).resolve()
            try:
                target.relative_to(root)
            except ValueError:
                skipped += 1
                continue
            if not target.is_file():
                skipped += 1
                continue
            bundle.write(target, arcname=target.name)
            included += 1
    payload = buffer.getvalue()
    if len(payload) > SELECTION_MAX_BYTES:
        raise SelectionTooLarge(len(payload))
    return {"data": payload, "included": included, "skipped": skipped}


def exports_dir(data_dir: Path) -> Path:
    return Path(data_dir) / EXPORTS_SUBDIR


def list_exports(data_dir: Path) -> list[dict]:
    """已有的备份文件（新→旧；同秒的按文件名倒序，保证清理顺序稳定）。"""
    directory = exports_dir(data_dir)
    if not directory.is_dir():
        return []
    items = []
    for path in directory.iterdir():
        if not path.is_file() or not _EXPORT_NAME_RE.match(path.name):
            continue
        stat = path.stat()
        items.append(
            {"name": path.name, "size": stat.st_size, "mtime": int(stat.st_mtime)}
        )
    # 先按名字倒序、再按 mtime 降序（稳定排序，所以同秒的批次保持名字倒序）：
    # 同一秒生成的两份备份 mtime 相同，只按 mtime 排会让「删哪份」变得不确定。
    items.sort(key=lambda item: item["name"], reverse=True)
    items.sort(key=lambda item: -item["mtime"])
    return items


def create_backup(data_dir: Path, db_path: Path, *, stem: str | None = None) -> Path:
    """生成一份完整备份，返回 zip 路径；同时清理超出保留数的旧备份。

    stem 允许调用方指定文件名主体（默认按当前时间）——测试里用来构造
    多份不同名字的备份，避免同一秒内两次生成互相覆盖。
    数据库用 SQLite backup API 取一致快照——WAL 模式下直接复制 .db 文件
    可能漏掉 -wal 里的最新写入，恢复出来就是旧数据。
    """
    data_dir = Path(data_dir)
    directory = exports_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    name = stem if stem else time.strftime(
        "memenest-backup-%Y%m%d-%H%M%S.zip", time.localtime()
    )
    if not _EXPORT_NAME_RE.match(name):
        raise ValueError(f"非法备份名: {name!r}")
    target = directory / name

    snapshot = directory / f".{name}.db.tmp"
    source = sqlite3.connect(db_path)
    try:
        dest = sqlite3.connect(snapshot)
        try:
            source.backup(dest)
        finally:
            dest.close()
    except Exception:
        # 快照失败必须自己收拾临时文件：它的名字不合备份规则，列表里看不见，
        # 留着就是一块永远没人知道的占盘。
        snapshot.unlink(missing_ok=True)
        raise
    finally:
        source.close()

    staged = directory / f".{name}.zip.tmp"
    try:
        # 图片本身已是压缩格式，STORED（只打包不压缩）快得多且体积几乎不变
        with zipfile.ZipFile(staged, "w", compression=zipfile.ZIP_STORED) as bundle:
            bundle.write(snapshot, "memo.db")
            for sub in _CONTENT_DIRS:
                root = data_dir / sub
                if not root.is_dir():
                    continue
                for path in sorted(root.rglob("*")):
                    if path.is_file():
                        bundle.write(path, f"{sub}/{path.relative_to(root).as_posix()}")
        # 先写临时名再改回来：中途失败/进程被杀不会留下一个「名字合法但内容截断」
        # 的备份被骗进列表里。
        os.replace(staged, target)
    finally:
        snapshot.unlink(missing_ok=True)
        staged.unlink(missing_ok=True)

    for old in list_exports(data_dir)[EXPORT_KEEP:]:
        try:
            (directory / old["name"]).unlink()
            logger.info("[MemeNest] 清理旧备份 %s", old["name"])
        except OSError:
            pass
    logger.info("[MemeNest] 备份完成 %s size=%s", target.name, target.stat().st_size)
    return target


def delete_export(data_dir: Path, name: str) -> bool:
    """按文件名删除一份备份；名字必须长得像备份（防路径穿越）。"""
    if not _EXPORT_NAME_RE.match(name or ""):
        return False
    path = exports_dir(data_dir) / name
    if not path.is_file():
        return False
    path.unlink()
    return True

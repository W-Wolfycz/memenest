"""导出备份。

守的静默失效：
- 直接复制 .db 而不是快照：WAL 里最新写入没进备份，恢复出来是旧数据，
  而且只有真出事要恢复时才发现；
- 旧备份无限堆积：备份目录慢慢吃满磁盘，没有任何提示；
- 删除接口不校验文件名：`../memo.db` 一样的路径穿越能删掉任意文件。
"""

from __future__ import annotations

import sqlite3
import zipfile

from memenest.backup import create_backup, delete_export, list_exports
from memenest.domain import STATE_READY, Analysis, Draft
from memenest.store import MemoStore


def _draft(rel_path: str) -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path),
        scope="any",
        origin_umo=None,
        origin_kind="import",
        created_at=1,
        state=STATE_READY,
    )


def _setup(tmp_path):
    data = tmp_path / "data"
    (data / "blobs").mkdir(parents=True)
    store = MemoStore(data / "memo.db")
    store.initialize()
    store.add(_draft("blobs/a.png"))
    (data / "blobs" / "a.png").write_bytes(b"png-bytes")
    (data / "inbox").mkdir()
    (data / "inbox" / "b.png").write_bytes(b"inbox-bytes")
    (data / "tmp").mkdir()
    (data / "tmp" / "junk.bin").write_bytes(b"junk")
    return data, store


def test_backup_contains_consistent_db_and_images(tmp_path):
    """备份要含可打开的 db 快照与 blobs/inbox；可再生的 tmp 不进。"""
    data, store = _setup(tmp_path)
    try:
        target = create_backup(data, data / "memo.db", stem="memenest-backup-20261113-010101.zip")
        assert target.is_file()
        with zipfile.ZipFile(target) as bundle:
            names = set(bundle.namelist())
        assert "memo.db" in names
        assert "blobs/a.png" in names
        assert "inbox/b.png" in names
        assert not any(name.startswith("tmp/") for name in names), "tmp 不该进备份"
        # 快照必须是可打开且带数据的（拦「复制了半截 db」）
        with zipfile.ZipFile(target) as bundle:
            snapshot = tmp_path / "restored.db"
            snapshot.write_bytes(bundle.read("memo.db"))
        conn = sqlite3.connect(snapshot)
        try:
            count = conn.execute("SELECT COUNT(*) FROM memo").fetchone()[0]
        finally:
            conn.close()
        assert count == 1
    finally:
        store.close()


def test_old_backups_beyond_keep_are_pruned(tmp_path):
    """只保留最近 EXPORT_KEEP 份：旧备份在生成新份时被清掉。"""
    data, store = _setup(tmp_path)
    try:
        for index in range(4):
            target = create_backup(
                data,
                data / "memo.db",
                stem=f"memenest-backup-2026010{index + 1}-000000.zip",
            )
            # 同秒创建的文件 mtime 相同，排序会失真；显式给出递增时间
            # （取 2023 年起的时间戳，确保全部早于第 4 份的真实创建时间）
            stamp = 1_700_000_000 + index * 60
            import os

            os.utime(target, (stamp, stamp))
        names = [item["name"] for item in list_exports(data)]
        assert len(names) == 3
        assert "memenest-backup-20260101-000000.zip" not in names
        assert "memenest-backup-20260104-000000.zip" in names
    finally:
        store.close()


def test_delete_export_rejects_traversal_names(tmp_path):
    """删除只接受长得像备份的文件名：`../memo.db` 这类必须被拒。

    校验写坏时 `../evil.txt` 会拼成 `exports/../evil.txt`＝数据目录里的真实文件，
    一旦存在就真的被删掉——所以陷阱文件必须放在会被命中的位置上。
    """
    data, store = _setup(tmp_path)
    try:
        evil = data / "evil.txt"  # exports/../evil.txt 的解析目标
        evil.write_text("x")
        assert delete_export(data, "../evil.txt") is False
        assert delete_export(data, "memo.db") is False
        assert evil.exists(), "越界删除必须被拦住"
        create_backup(
            data, data / "memo.db", stem="memenest-backup-20260101-000000.zip"
        )
        assert delete_export(data, "memenest-backup-20260101-000000.zip") is True
        assert list_exports(data) == []
    finally:
        store.close()

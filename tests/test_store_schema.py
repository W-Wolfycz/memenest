"""库结构与级联：建表、外键删除、黑名单分页、老库原地升级。"""

from __future__ import annotations

import sqlite3

import pytest

from memenest.domain import STATE_READY, Analysis, Draft
from memenest.store import MemoStore, StoreError


def _draft(rel_path: str, *, state: str = STATE_READY, persona: str | None = None) -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path, labels=["smug"]),
        scope="any",
        origin_umo=None,
        origin_kind="chat",
        created_at=1,
        state=state,
        persona_ids=(persona,) if persona else (),
    )

def test_delete_cascades_to_labels(tmp_path):
    """外键级联关掉后，删掉的条目仍留在关联表里，分类分布会虚高。"""
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    try:
        memo_id = store.add(_draft("blobs/cascade.png"))
        assert memo_id is not None
        assert store.labels_in_use()  # 附加标签已写入
        store.delete(memo_id)
        assert store.labels_in_use() == {}
    finally:
        store.close()


def _set_version(db_path, value: str) -> None:
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE meta SET value = ? WHERE key = 'schema_version'", (value,))
    conn.commit()
    conn.close()


def _version(db_path) -> str:
    conn = sqlite3.connect(db_path)
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    conn.close()
    return str(row[0])


def test_old_database_is_migrated_in_place(tmp_path):
    """老库（结构版本更小）要原地升级，而不是要求用户删库。

    删库会丢掉用户攒的表情；只有「结构比插件新」才该拒绝启动。
    """
    db_path = tmp_path / "memo.db"
    store = MemoStore(db_path)
    store.initialize()
    memo_id = store.add(_draft("blobs/old.png"))
    store.close()

    _set_version(db_path, "1")
    store = MemoStore(db_path)
    store.initialize()
    try:
        assert store.get(memo_id) is not None, "升级不能丢数据"
        assert _version(db_path) == "2"
    finally:
        store.close()


def test_future_database_version_is_refused(tmp_path):
    """结构比插件新时拒绝启动：降级跑未知结构只会把库写坏。"""
    db_path = tmp_path / "memo.db"
    store = MemoStore(db_path)
    store.initialize()
    store.close()

    _set_version(db_path, "99")
    store = MemoStore(db_path)
    with pytest.raises(StoreError):
        store.initialize()


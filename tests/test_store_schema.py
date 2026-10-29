"""库结构与级联：建表、外键删除、黑名单分页。"""

from __future__ import annotations

from memenest.domain import STATE_READY, Analysis, Draft
from memenest.store import MemoStore


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


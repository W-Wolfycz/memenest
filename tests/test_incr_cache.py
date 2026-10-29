"""增量需求：检索加速——候选集缓存与写操作失效。

守的静默失效：缓存永不失效（新增/删除的表情一直看不到）、
或者每次发送都重新查库（检索退化成全表扫描）。
"""

from __future__ import annotations

import pytest

from memenest.config import Settings
from memenest.domain import STATE_READY, Analysis, Draft
from memenest.publish.candidates import CandidateCache
from memenest.store import MemoStore


def _draft(rel_path: str, persona_ids: tuple[str, ...] = ()) -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path),
        scope="any",
        origin_umo="aiocqhttp:GroupMessage:1",
        origin_kind="chat",
        created_at=1,
        state=STATE_READY,
        persona_ids=persona_ids,
    )


@pytest.fixture()
def store(tmp_path):
    instance = MemoStore(tmp_path / "memo.db")
    instance.initialize()
    yield instance
    instance.close()


@pytest.mark.asyncio
async def test_cache_is_keyed_by_persona(store):
    """缓存按人格分键：混用会让 A 人格的池子里混进 B 人格的表情，跨人格发出去。"""
    store.add(_draft("inbox/generic.png"))
    store.add(_draft("inbox/for_p1.png", ("p1",)))
    cache = CandidateCache(store, lambda: Settings(), clock=lambda: 0.0)
    assert len(await cache.get(persona_id="p1")) == 2
    assert len(await cache.get(persona_id="p2")) == 1
    assert cache.stats()["缓存未命中"] == 2


@pytest.mark.asyncio
async def test_cache_loads_every_ready_row(store):
    """预载上限被 store 截断 = 库超过阈值后一半表情永远发不出来。"""
    for index in range(1, 421):
        store.add(_draft(f"blobs/202601/{index:06d}.png"))
    cache = CandidateCache(store, lambda: Settings(), clock=lambda: 0.0)
    items = await cache.get(persona_id=None)
    assert len(items) == 420

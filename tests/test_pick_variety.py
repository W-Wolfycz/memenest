"""选图变化程度（pick_variety）。

守的静默失效：
- 配置项被默默忽略：三档行为完全一样，用户调了界面上没反应也看不出来；
- 「线」没生效：分数明显不够的图也有机会被选中，表现成偶发的图文不配，
  用户多半归因于「表情包本来就这样」而不是配置错了。
"""

from __future__ import annotations

import asyncio
import random

import pytest

from memenest import embed as emb
from memenest.config import VARIETY_DELTA, load_settings
from memenest.domain import STATE_READY, Analysis, CandidateQuery, Draft
from memenest.publish.dispatcher import Dispatcher
from memenest.publish.vectors import VectorStore
from memenest.store import MemoStore


class _Provider:
    """8 维基向量：每张图与查询的夹角依次变大。"""

    def __init__(self, table: dict) -> None:
        self.table = table
        self.dim = 8

    def get_dim(self) -> int:
        return self.dim

    async def get_embedding(self, text: str):
        return self.table.get(text, [0.0] * self.dim)

    async def get_embeddings(self, texts):
        return [await self.get_embedding(t) for t in texts]


class _Log:
    def debug(self, *args, **kwargs) -> None:
        pass

    info = warning = error = debug


TABLE = {
    "话题A": [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "话题B": [1.0, 0.20, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "话题C": [1.0, 0.40, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
    "话题D": [1.0, 2.00, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
}
SCENES = [("话题A",), ("话题B",), ("话题C",), ("话题D",)]


async def _picks(tmp_path, variety: str, rounds: int = 120) -> list[int]:
    store = MemoStore(tmp_path / f"{variety}.db")
    store.initialize()
    provider = _Provider(TABLE)
    embedder = emb.Embedder(provider=provider, model="m", timeout=5.0)
    for index, scenes in enumerate(SCENES, start=1):
        memo_id = store.add(
            Draft(
                rel_path=f"blobs/{index}.png",
                content_hash=f"{index:064x}",
                percept_hash=None,
                analysis=Analysis(label="blank", caption="", scenes=tuple(scenes)),
                scope="any",
                origin_umo=None,
                origin_kind="import",
                created_at=index,
                state=STATE_READY,
            )
        )
        parts = emb.vectors_for(text_in_image="", caption="", scenes=scenes)
        if parts["scene"]:
            vec = emb.normalize(await provider.get_embedding(parts["scene"]))
            store.save_embeddings([(memo_id, "scene", "m", len(vec), emb.pack(vec))], 1)

    settings = load_settings(
        {
            "embedding_enabled": True,
            "embedding_threshold": 0.0,
            "weight_text": 0.0,
            "weight_caption": 0.0,
            "weight_scenes": 1.0,
            "weight_label": 0.0,
            "pick_variety": variety,
        }
    )
    dispatcher = Dispatcher(
        store=store,
        data_dir=tmp_path,
        settings=lambda: settings,
        log=_Log(),
        deliver=lambda *a, **k: asyncio.sleep(0),
        embedder=lambda: embedder,
        vectors=VectorStore(store),
        rng=random.Random(7),
    )
    pool = store.candidates(
        CandidateQuery(state=STATE_READY, umo=None, ignore_scope=True, limit=50)
    )
    out = []
    for _ in range(rounds):
        memo, _ = await dispatcher._pick_by_vector(
            pool, "话题A", "", "umo", [], None, False
        )
        out.append(memo.id if memo else None)
    store.close()
    return out


@pytest.mark.asyncio
async def test_pick_variety_changes_spread_and_respects_floor(tmp_path):
    """三档必须真的不一样，且分数明显不够的图不能中。

    两件事一起测是因为它们是同一条链上的：档位读错 → 三档一样；
    floor 算错 → 线外的图也进来。前者用户看不出（调了没反应），
    后者会表现成偶发的图文不配。
    """
    stable = await _picks(tmp_path, "stable")
    lively = await _picks(tmp_path, "lively")

    assert len(set(stable)) < len(set(lively)), "lively 必须比 stable 更分散"
    assert len(set(stable)) >= 1

    # #4 是故意做到「分数低于最高分 - 0.10」的那张，三个档位都不该出现
    for picks in (stable, lively):
        assert 4 not in picks, "线外（#4）不该被选中"

    # 最高分那张在任何档位都该是最常见的，而且 stable 档下要占压倒性多数。
    # 只断言「最常见的是 #1」拦不住「权重被改成均匀」：均匀采样时 #1 也是三者之一，
    # 但占比会从八成掉到三成出头。
    from collections import Counter

    for picks in (stable, lively):
        counter = Counter(picks)
        assert counter.most_common(1)[0][0] == 1
    stable_top_share = Counter(stable)[1] / len(stable)
    assert stable_top_share > 0.8, (
        f"stable 档下最高分那张只占 {stable_top_share:.0%}，"
        "说明权重没起作用（退化成均匀采样了）"
    )


def test_variety_delta_is_ordered():
    """档位对应的窗口必须递增：写反了界面上「活泼」会变成最稳的那个。"""
    assert VARIETY_DELTA["stable"] < VARIETY_DELTA["balanced"] < VARIETY_DELTA["lively"]

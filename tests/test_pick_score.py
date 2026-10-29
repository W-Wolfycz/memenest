"""候选打分与下限。

守的静默失效：
- `min_score` 的「0 = 不设限」被写坏成「0 也拒」，小库刚用过一轮就永远不发图；
- 话题词两路命中被重复加分，同一句话多拿一份权重，排序被抬高；
- 作用域/人格把候选滤光时被报成「不够相关」，面板统计口径错乱。
"""

from __future__ import annotations

import random

import pytest

from memenest.domain import STATE_READY, SCOPE_ORIGIN, Analysis, Draft
from memenest.publish.pick import (
    PickContext,
    pick_best,
    score_memo,
)
from memenest.store import MemoStore


def _make(store: MemoStore, index: int, **kw) -> int:
    return store.add(
        Draft(
            rel_path=f"blobs/{index}.png",
            content_hash=str(index) * 64,
            percept_hash=None,
            analysis=Analysis(
                label=kw.get("label", "blank"),
                caption=kw.get("caption", "少女"),
                text_in_image=kw.get("text", ""),
                scenes=tuple(kw.get("scenes", ())),
            ),
            scope=kw.get("scope", "any"),
            origin_umo=kw.get("origin"),
            origin_kind="import",
            created_at=index,
            state=STATE_READY,
        )
    )


def _ctx(**kw) -> PickContext:
    base = {"umo": "G:1", "rng": random.Random(7)}
    base.update(kw)
    return PickContext(**base)


def test_min_score_zero_never_rejects_negative_scores(tmp_path):
    """`min_score=0` 表示不设限。

    刚用过一轮的小库 recency 惩罚能把整池压到负分（最高 -4.0），
    这时如果拿 0 去比较，所有候选都被当成「不够相关」，一张都发不出来，
    而且日志只说「低于 0.00」——从用户视角就是「表情窝突然不工作了」。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    ids = [_make(store, i, caption=f"少女{i}") for i in range(1, 4)]
    pool = [store.get(i) for i in ids]
    recent = list(ids)  # 全部算「最近用过」

    ctx = _ctx(recent_ids=recent, min_score=0.0)
    assert score_memo(pool[0], ctx) < 0, "前置条件：这份数据必须是负分"
    memo, below = pick_best(pool, ctx)
    assert memo is not None, "min_score=0 时负分也必须照发"
    assert below is False

    # 真的开了下限才该拒
    strict = _ctx(recent_ids=recent, min_score=2.0)
    memo2, below2 = pick_best(pool, strict)
    assert memo2 is None
    assert below2 is True
    store.close()


def test_topic_hit_scores_once_not_twice(tmp_path):
    """话题词两路都命中时只加一次分。

    用户消息和机器人回复常常是同一件事（「变猫娘」），如果两条分支都加
    `weights.scene`，这张图的排序会被凭空抬高，压过真正更合适的候选。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    memo_id = _make(store, 1, caption="少女冒汗", scenes=("变猫娘",))
    memo = store.get(memo_id)

    user_only = _ctx(context_keywords=("变猫娘",), keywords=())
    reply_only = _ctx(context_keywords=(), keywords=("变猫娘",))
    both = _ctx(context_keywords=("变猫娘",), keywords=("变猫娘",))
    neither = _ctx(context_keywords=("天气",), keywords=("天气",))

    s_user = score_memo(memo, user_only)
    s_reply = score_memo(memo, reply_only)
    s_both = score_memo(memo, both)
    s_none = score_memo(memo, neither)

    assert s_user > s_none, "命中了就该比没命中高"
    assert s_both == pytest.approx(s_user), "两路都命中不能比单路更高"
    assert s_both == pytest.approx(s_reply)
    store.close()


def test_scope_filtered_out_is_not_low_relevance(tmp_path):
    """候选被作用域滤光时是「没有候选」，不是「不够相关」。

    这两者混在一个 reason 里，面板上「拒收统计」的语义就乱了：
    用户配了 `限来源` 却看到一堆「不够相关」，会去调错配置项。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    _make(store, 1, caption="别群的图", scope=SCOPE_ORIGIN, origin="G:999")
    pool = [store.get(1)]

    ctx = _ctx(umo="G:1", min_score=2.0)
    memo, below = pick_best(pool, ctx)
    assert memo is None
    assert below is False, "滤光了不该标记成「不够相关」"
    store.close()

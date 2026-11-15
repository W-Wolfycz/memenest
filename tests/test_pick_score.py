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
    category_recent_penalty,
    pick_best,
    score_memo,
)
from memenest.publish.punctuation import expected_labels
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


def test_category_recent_penalty_demotes_only_the_same_label(tmp_path):
    """同一情绪刚发过的那几张要排后面，别的分类不受影响。

    没有这层时，连续两句同情绪的话会一直抽到同一张——看起来像随机性失灵；
    惩罚跨类生效则会误伤「这次真的是另一类情绪」的候选。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    first = _make(store, 1, label="joy", caption="猫在笑")
    second = _make(store, 2, label="joy", caption="猫在笑")
    other = _make(store, 3, label="sad", caption="猫在哭")
    a, b, c = store.get(first), store.get(second), store.get(other)

    # 同类竞争：1 刚用过，同分时应该选 2
    ctx = _ctx(recent_by_label={"joy": (first,)})
    assert score_memo(a, ctx) < score_memo(b, ctx)
    memo, _ = pick_best([a, b], ctx)
    assert memo is not None and memo.id == second

    # 跨类不惩罚：sad 那张的分数不该被 joy 的最近使用拖低
    ctx2 = _ctx(recent_by_label={"joy": (first,)})
    assert score_memo(c, ctx2) == pytest.approx(score_memo(c, _ctx()))
    # 超出窗口（第 4 名之后）不再扣分
    assert category_recent_penalty((first, second, other, 5), 5, 0.8) > 0
    assert category_recent_penalty((first, second, other, 5, 9), 9, 0.8) == 0.0
    store.close()


def test_recent_by_label_honours_scan_window(tmp_path):
    """每分类最近使用的扫描窗口必须生效。

    这个查询每条消息都会跑一次；去掉 LIMIT 就是「按使用时间排全表」，
    库大一点就变成热点查询，而表现只是「回复越来越慢」。
    """
    store = MemoStore(tmp_path / "memo.db")
    store.initialize()
    labels = ["joy", "sad", "upset", "blank", "cry"]
    ids = [_make(store, i, label=labels[i - 1]) for i in range(1, 6)]
    for memo_id in ids:
        store.bump_usage(memo_id, 1_700_000_000 + memo_id)
    try:
        # 五个不同分类各 1 张：窗口 3 → 最多回 3 个分类；窗口 40 → 5 个都在
        assert len(store.recent_ids_by_label(1, 3)) == 3
        assert len(store.recent_ids_by_label(1, 40)) == 5
    finally:
        store.close()


def test_punctuation_prior_only_adds_a_weak_signal():
    """标点先验是弱信号：能给方向，但不能盖过真正命中。

    写强了会让「一句话里带问号」永远压过图上写着同一句话的图，
    写没了则等于这个维度不存在。
    """
    assert "puzzle" in expected_labels("真的吗？？")
    assert "wow" in expected_labels("这也行？！")
    assert "cheer" in expected_labels("太强了！")
    assert {"shy", "love"} <= expected_labels("好耶~")
    assert {"blank", "slack"} <= expected_labels("算了……")
    # 引号/代码/URL 里的标点不算机器人自己的语气
    assert expected_labels("他说「真的吗？」") == set()
    assert expected_labels("看这个 https://x.com/a?b=1") == set()

    # 先验用的标签 key 必须都在标签体系里：标错一个字母，这条规则就永久失效，
    # 而且不报错——只有比对能发现。
    from memenest.domain import LABEL_KEYS  # noqa: PLC0415

    everything = expected_labels("真的吗？？！！……好耶~")
    assert everything, "前置条件：这句话要能触发全部规则"
    assert everything <= set(LABEL_KEYS)

    # 有加分，但是小量：不能盖过关键词/话题命中
    from _fixtures import make_memo  # noqa: PLC0415

    memo = make_memo(99, label="joy")
    delta = score_memo(memo, _ctx(expected_labels=frozenset({"joy"}))) - score_memo(
        memo, _ctx()
    )
    assert 0 < delta <= 0.5

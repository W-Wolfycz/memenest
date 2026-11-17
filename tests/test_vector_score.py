"""向量路的打分口径（`score_vector_entry`）。

守的静默失效：
- 分母按「库里最全的一条」算而不是「这条实际有哪几路」算：没有话题词的图
  （立绘、壁纸）会永久少一截分，同分时永远排在有词图后面；
- 缺一路时拿 0.0 兜底：余弦可能为负，垫 0 等于偷偷给那一路加了个 ReLU，
  和别的路口径不一致；
- 标签/先验/同类避让的加成乘错权重：调了权重但排序没变，面板上完全看不出来。
"""

from __future__ import annotations

import pytest

from _fixtures import make_memo
from memenest.config import WEIGHT_CAPTION, WEIGHT_LABEL, WEIGHT_TEXT
from memenest.publish.dispatcher import score_vector_entry


def _kinds(text=None, caption=None, scene=None) -> dict:
    kinds: dict = {}
    if text is not None:
        kinds["text"] = text
    if caption is not None:
        kinds["caption"] = caption
    if scene is not None:
        kinds["scene"] = scene
    return kinds


def _vec(value: float) -> tuple[float, ...]:
    """单维向量：与 `_vec(1.0)` 查询的点积就等于 value。"""
    return (float(value),)


def test_denominator_counts_only_present_lanes():
    """缺一路只影响这一条的分母，不影响别的条目。

    写法改成「固定按三路归一」时，没有话题词的图会整体低 0.3/(0.3+0.25+0.15)，
    看起来像「检索效果差」，其实是口径错了。
    """
    memo = make_memo(1, label="joy")
    query = _vec(1.0)
    # 三路全有：分母 0.15+0.25+0.30+0.10
    full = score_vector_entry(
        memo,
        _kinds(text=_vec(1.0), caption=_vec(1.0), scene=_vec(1.0)),
        query,
        None,
        label_bonus=1.0,
    )
    assert full == pytest.approx(1.0), "三路都命中时加权平均应该正好是 1.0"
    # 只有 caption：分母 0.25+0.10，命中 1.0 → 归一化后仍是 1.0
    only_caption = score_vector_entry(
        memo, _kinds(caption=_vec(1.0)), query, None, label_bonus=1.0
    )
    assert only_caption == pytest.approx(1.0), "缺路不能压低这一条的分"
    # 权重比例必须对得上：只有 text 命中时，归一化后仍是 1.0
    only_text = score_vector_entry(
        memo, _kinds(text=_vec(1.0)), query, None, label_bonus=1.0
    )
    assert only_text == pytest.approx(1.0)


def test_negative_similarity_is_not_floored():
    """余弦为负时照常按负值算，不能垫成 0。

    垫 0 会让「完全不像」和「有点不像」拿到同样的分，排序被悄悄压平。
    """
    memo = make_memo(2, label="joy")
    score = score_vector_entry(
        memo, _kinds(caption=_vec(-1.0)), _vec(1.0), None, label_bonus=0.0
    )
    # 只有 caption 一路：分母 0.25 + 0.10(label)，value = 0.25 * -1 = -0.25
    assert score == pytest.approx(-0.25 / (WEIGHT_CAPTION + WEIGHT_LABEL))



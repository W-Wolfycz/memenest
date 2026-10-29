"""T-09 / T-10 / P-1~P-5：模型输出的解析契约。

守的静默失效：模型多说了几句就被判成失败（收不到表情），
或者字段越界没被归一（标签体系出现孤儿值，界面与检索全部失准）。
"""

from __future__ import annotations

import json

from memenest.domain import (
    LABEL_ALIASES,
    LABEL_KEYS,
    MAX_CAPTION,
    MAX_TEXT_IN_IMAGE,
    normalize_label,
)
from memenest.ingest.analyze import clean_list, parse_analysis


def test_T09_non_json_output_is_a_failure():
    """非 JSON 一律判失败：当成功会让整库条目带着兜底分类落库，用户以为识图一直在正常工作。"""
    assert parse_analysis("这张图很搞笑，我觉得是开心") is None
    assert parse_analysis("") is None
def test_all_aliases_point_to_real_labels():
    """中文别名必须指向真实存在的 key。

    指向不存在的 key（历史上「感谢」就是这么写的）会让这类条目带着非法标签入库，
    面板的分类筛选里查不到它，也不会报错——属于纯静默的数据污染。
    """
    dangling = {
        alias: target
        for alias, target in LABEL_ALIASES.items()
        if target not in LABEL_KEYS
    }
    assert not dangling, f"这些别名指向了不存在的 key: {dangling}"

def test_clean_list_keeps_list_items_intact():
    """列表元素里的逗号是句内停顿，不能再切开。

    切开会让条数凭空变多、撞上 MAX_SCENES 上限挤掉别的词——用户只会觉得
    「这条表情的话题怎么少了一个」，没有任何报错。
    """
    items = clean_list(["摸鱼，爽", "被骂"], 3)
    assert items == ["摸鱼，爽", "被骂"]
    # 超过单条字数上限的整条丢弃，不截断也不切碎
    assert clean_list(["变猫娘", "这需求文档写得什么鬼"], 3) == ["变猫娘"]

    # 整个字段是字符串时才按标点切
    assert clean_list("我裂开了,笑死", 3) == ["我裂开了", "笑死"]


def test_normalize_label_tolerates_chatty_output():
    """模型多写点东西不该把整条丢成 other。

    提示词自己把 key 渲染成 `disgust(嫌弃/不屑)`，模型照抄这个格式很常见；
    写成「开心、嫌弃」或数组也一样。
    """
    assert normalize_label("disgust(嫌弃/不屑)") == "disgust"
    assert normalize_label("joy, disgust") == "joy"
    assert normalize_label("开心、嫌弃") == "joy"   # 取第一个能认出的
    assert normalize_label("joy;disgust") == "joy"
    assert normalize_label(["blank", "refuse"]) == "blank"
    assert normalize_label("完全不认识的东西") == "other"


def test_text_in_image_keeps_long_lines():
    """图内台词上限 200 字，不能和 caption 共用 80。

    梗图里整段对话都有；截到 80 字后按原句检索匹配不到后半句，
    面板上看不出问题。
    """
    long_text = "台词" * 150
    analysis = parse_analysis(
        json.dumps({"label": "joy", "text_in_image": long_text, "caption": long_text})
    )
    assert analysis is not None
    assert len(analysis.text_in_image) == MAX_TEXT_IN_IMAGE
    assert len(analysis.caption) == MAX_CAPTION
    assert MAX_TEXT_IN_IMAGE > MAX_CAPTION

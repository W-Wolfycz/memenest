"""T-09 / T-10 / P-1~P-5：模型输出的解析契约。

守的静默失效：模型多说了几句就被判成失败（收不到表情），
或者字段越界没被归一（标签体系出现孤儿值，界面与检索全部失准）。
"""

from __future__ import annotations

from memenest.domain import (
    LABEL_ALIASES,
    LABEL_KEYS,
    normalize_label,
)
from memenest.ingest.analyze import clean_list


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

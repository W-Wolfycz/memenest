"""T-02 / R-1：判定树。

守的静默失效：普通照片被当表情收进库（库被垃圾慢慢污染），
或者判定不合格却消耗了节流窗口（群里聊图多时表情再也收不到）。
"""

from __future__ import annotations

from _fixtures import FakeEvent

from memenest.ingest.collect import collect_refs


def _image(url: str = "https://cdn.example.com/photo.jpg", **data) -> dict:
    payload = {"url": url, "file": "photo.jpg", **data}
    return {"type": "image", "data": payload}

def test_judgment_tree_priorities():
    """判定树：mface > sub_type > summary > CDN 特征，都不命中就丢；accept_all 全收。

    漏判是完全静默的——那张表情就是没进库，日志里只留一行 DEBUG。
    """

    def judged(segment, accept_all=False):
        outcome = collect_refs(FakeEvent(raw_segments=[segment]), accept_all=accept_all)
        return [ref.reason for ref in outcome.refs], outcome.reason

    # mface：平台直接标了表情，不再看其它标记
    assert judged({"type": "mface", "data": {"url": "https://x.example.com/a.png"}}) == (
        ["sticker_mface"],
        "sticker_mface",
    )
    # 段里的 sub_type 标记
    assert judged(_image(sub_type="1")) == (["sticker_subtype"], "sticker_subtype")
    # summary 文案里的「表情」字样
    assert judged(_image(summary="动画表情")) == (["sticker_summary"], "sticker_summary")
    # 图床特征兜底
    assert judged(_image(url="https://gxh.vip.qq.com/x.png")) == (
        ["sticker_cdn"],
        "sticker_cdn",
    )
    # 三个标记都没有：普通照片，丢弃
    assert judged(_image(url="https://cdn.example.com/photo.jpg")) == (
        [],
        "no_sticker_mark",
    )
    # accept_all 绕过整套判定
    assert judged(_image(url="https://cdn.example.com/photo.jpg"), accept_all=True) == (
        ["collect_all"],
        "collect_all",
    )
    # mface 比 sub_type 更优先
    assert judged(
        {"type": "mface", "data": {"url": "https://cdn.example.com/a.png", "sub_type": "1"}}
    ) == (["sticker_mface"], "sticker_mface")

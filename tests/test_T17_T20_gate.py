"""R-4：作用域门控。

守的静默失效：作用域取不到会话标识时放行——「只想在这个群用」的图会被发到所有会话，
而发出去的图本身看不出是从哪来的。
"""

from __future__ import annotations

from memenest.domain import GENERIC_PERSONA, SCOPE_ANY, SCOPE_ORIGIN, Memo
from memenest.publish.gate import humanized_delay, visible_length
from memenest.publish.pick import scope_allows


def _memo(memo_id: int, *, scope: str = SCOPE_ANY, origin: str | None = "g:1") -> Memo:
    return Memo(
        id=memo_id,
        rel_path=f"blobs/{memo_id}.png",
        content_hash=f"{memo_id:064x}",
        percept_hash=None,
        label="joy",
        caption="猫在笑",
        text_in_img="",
        scope=scope,
        origin_umo=origin,
        state="ready",
        is_fav=False,
        use_count=0,
        created_at=1,
        used_at=0,
        origin_kind="chat",
        persona_ids=(GENERIC_PERSONA,),
    )


def test_T18_scope_requires_matching_session_and_fails_closed():
    """仅来源的图只能在原会话发；取不到会话标识时一律拒绝。

    判定写松了（跨会话放行、或把 None 当通配）等于把「只想在这个群用」的图撒到所有群，
    而发出去的图本身看不出是从哪来的。
    """
    memo = _memo(1, scope=SCOPE_ORIGIN, origin="aiocqhttp:GroupMessage:1")
    assert scope_allows(memo, "aiocqhttp:GroupMessage:1") is True
    assert scope_allows(memo, "aiocqhttp:GroupMessage:2") is False
    # 取不到会话标识 = fail-closed，不能当成通配
    assert scope_allows(_memo(3, scope=SCOPE_ORIGIN), None) is False
    assert scope_allows(_memo(3, scope=SCOPE_ORIGIN), "") is False
    # 全库可用的图不受影响
    assert scope_allows(_memo(2), None) is True


def test_humanized_delay_bounds_and_zero_base():
    """延迟拟人化的两条硬边界：base=0 必须保持 0（用户要立即发），
    其余情况要落在 [base*0.75+文本因子, base*1.25+文本因子] 内。

    写坏成「base=0 也加抖动」会让关延迟的用户开始看到延迟；
    抖动系数跑飞则回到固定秒数，等于没做。
    """
    assert humanized_delay(0, "随便说点什么", 0.5) == 0.0
    text = "今天这场雨下得也太大了，出门就湿透了"
    extra = min(2.0, visible_length(text) / 30.0)
    low = humanized_delay(5, text, 0.0)
    high = humanized_delay(5, text, 1.0)
    assert low == 5 * 0.75 + extra
    assert high == 5 * 1.25 + extra
    mid = humanized_delay(5, text, 0.5)
    assert low < mid < high

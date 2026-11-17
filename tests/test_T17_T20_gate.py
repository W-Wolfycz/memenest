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



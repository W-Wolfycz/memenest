"""T-17 / T-18 / T-19 / T-20 / R-3 / R-4：闸顺序、意图门控与作用域。

守的静默失效：闸顺序被调换后概率/候选把便宜的拒绝挡在后面（无谓查库）；
作用域取不到会话标识时放行（越界发送）；命令回复也被补表情（打断指令交互）。
"""

from __future__ import annotations

from memenest.domain import GENERIC_PERSONA, SCOPE_ANY, SCOPE_ORIGIN, Memo
from memenest.publish.gate import GateInput
from memenest.publish.pick import scope_allows


class _Plain:
    type = "Plain"

    def __init__(self, text: str) -> None:
        self.text = text


class _Image:
    type = "Image"

    def __init__(self, file: str = "a.png") -> None:
        self.file = file


def _gate(**overrides) -> GateInput:
    base = {
        "auto_send_enabled": True,
        "turn_used": False,
        "turn_cancelled": False,
        "session_allowed": True,
        "session_cooling": False,
        "text": "这场雨下得太大了吧",
        "ratio": 1.0,
        "roll": 0.0,
        "has_candidate": True,
    }
    base.update(overrides)
    return GateInput(**base)


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


def test_T17_origin_scope_only_allowed_in_its_session():
    """仅来源的图只能在原会话发：判定写松了会把「只想在这个群用」的图撒到所有群。"""
    memo = _memo(1, scope=SCOPE_ORIGIN, origin="aiocqhttp:GroupMessage:1")
    assert scope_allows(memo, "aiocqhttp:GroupMessage:1") is True
    assert scope_allows(memo, "aiocqhttp:GroupMessage:2") is False


def test_T18_missing_session_identifier_fails_closed():
    """取不到会话标识时必须拒绝：fail-open 等于把「仅来源」的限制整体取消。"""
    memo = _memo(1, scope=SCOPE_ORIGIN)
    assert scope_allows(memo, None) is False
    assert scope_allows(memo, "") is False
    assert scope_allows(_memo(2), None) is True

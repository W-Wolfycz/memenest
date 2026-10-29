"""LLM 工具的纯逻辑（F6）：候选编号只在回合内有效，不写持久存储。

工具入口拿到的编号来自上一轮检索；回合切换（同一会话来了新消息）即失效。
"""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass

from .domain import MAX_CANDIDATES, Memo, Reason, label_display

FAIL_PREFIX = "表情窝工具失败"
MAX_TRACKED_SESSIONS = 512


@dataclass(frozen=True, slots=True)
class Handle:
    token: str
    memo: Memo


class HandleRegistry:
    """按会话保存最近一次检索结果；只存在于内存，进程重启即清空。"""

    def __init__(self, max_sessions: int = MAX_TRACKED_SESSIONS) -> None:
        self._slots: OrderedDict[str, list[Handle]] = OrderedDict()
        self._max_sessions = max(1, max_sessions)

    def issue(self, umo: str, memos: list[Memo]) -> list[Handle]:
        handles = [
            Handle(token=str(index + 1), memo=memo)
            for index, memo in enumerate(memos[:MAX_CANDIDATES])
        ]
        self._slots[umo] = handles
        self._slots.move_to_end(umo)
        while len(self._slots) > self._max_sessions:
            self._slots.popitem(last=False)
        return handles

    def resolve(self, umo: str, token: str) -> Memo | None:
        for handle in self._slots.get(umo, []):
            if handle.token == str(token).strip():
                return handle.memo
        return None

    def has_session(self, umo: str) -> bool:
        return umo in self._slots

    def invalidate(self, umo: str | None) -> bool:
        if not umo:
            return False
        return self._slots.pop(umo, None) is not None

    def clear(self) -> None:
        self._slots.clear()

    def size(self) -> int:
        return len(self._slots)


def render_candidates(handles: list[Handle]) -> str:
    if not handles:
        return f"{FAIL_PREFIX}（{Reason.NO_CANDIDATE}）：库里没有可用表情。"
    lines = [f"找到 {len(handles)} 个候选，用编号发送："]
    for handle in handles:
        memo = handle.memo
        pieces = [f"{handle.token}. [{label_display(memo.label)}]"]
        if memo.caption:
            pieces.append(memo.caption)
        elif memo.text_in_img:
            pieces.append(f"图上写着「{memo.text_in_img}」")
        else:
            pieces.append("（没有描述）")
        lines.append(" ".join(pieces))
    return "\n".join(lines)


def render_failure(reason_code: str, hint: str = "") -> str:
    text = f"{FAIL_PREFIX}（{reason_code}）"
    return f"{text}：{hint}" if hint else text


def render_sent(memo: Memo) -> str:
    label = label_display(memo.label)
    text = memo.caption or memo.text_in_img or "（无描述）"
    return f"已发出编号 {memo.id} 的表情（{label}）：{text}"


__all__ = [
    "FAIL_PREFIX",
    "Handle",
    "HandleRegistry",
    "render_candidates",
    "render_failure",
    "render_sent",
]

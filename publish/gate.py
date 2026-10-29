"""发送六道闸（R-3）：纯函数，顺序不可调换，任一拒绝即结束本轮。

闸序与理由：
1 总开关（最便宜）→ 2 回合状态（幂等）→ 3 会话策略（廉价内存判断）
→ 4 文本形态（要读文本，仍是纯函数）→ 5 概率（随机性放后面，便于复现调试）
→ 6 候选存在（最贵，最后做）。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from ..domain import Reason

DEFAULT_COMMAND_MARKERS: tuple[str, ...] = ("/", "!", "#", "！", "／", ".", "。")
QUESTION_MARKS = ("?", "？")
_STRIP_FOR_LENGTH = re.compile(r"[\s\W_]+", re.UNICODE)
_PLAIN_TYPE = "Plain"
_IMAGE_TYPE = "Image"


@dataclass(frozen=True, slots=True)
class GateInput:
    auto_send_enabled: bool
    turn_used: bool
    turn_cancelled: bool
    session_allowed: bool
    session_cooling: bool
    text: str
    ratio: float
    roll: float
    has_candidate: bool
    command_markers: tuple[str, ...] = DEFAULT_COMMAND_MARKERS
    reply_has_image: bool = False


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    code: str
    detail: str = ""


_ALLOWED = GateDecision(True, Reason.OK)


def session_key(umo: str | None) -> str | None:
    """把会话标识归一成「群 `G:<群号>` / 私聊 `F:<好友号>`」。

    两种输入都认：完整 UMO（`platform:GroupMessage:123`）和已经写好的 `G:123`。
    这样手填的配置和运行时取到的会话能直接比较，不必关心是哪台 BOT。
    """
    text = (umo or "").strip()
    if not text:
        return None
    # 已经是 G:/F: 写法：校验后半段是数字就直接用
    if len(text) > 2 and text[0].upper() in ("G", "F") and text[1] == ":":
        tail = text[2:].strip()
        return f"{text[0].upper()}:{tail}" if tail.isdigit() else None
    parts = text.split(":", 2)
    if len(parts) < 3:
        return None
    message_type = parts[1].lower()
    if "group" in message_type:
        kind = "G"
    elif "friend" in message_type:
        kind = "F"
    else:
        return None
    session = parts[2].strip()
    if not session:
        return None
    return f"{kind}:{session}"


def _component_type(component: Any) -> str:
    value = getattr(component, "type", None)
    return str(getattr(value, "value", value) or "")


def reply_text(chain: list[Any] | None) -> str:
    """把回复链里的纯文本拼起来；不读图片、语音等组件内容。"""
    if not chain:
        return ""
    parts: list[str] = []
    for component in chain:
        if _component_type(component) != _PLAIN_TYPE:
            continue
        text = str(getattr(component, "text", "") or "").strip()
        if text:
            parts.append(text)
    return "\n".join(parts).strip()


def reply_contains_image(chain: list[Any] | None) -> bool:
    if not chain:
        return False
    return any(_component_type(component) == _IMAGE_TYPE for component in chain)


def visible_length(text: str) -> int:
    return len(_STRIP_FOR_LENGTH.sub("", text or ""))


def question_count(text: str) -> int:
    return sum((text or "").count(mark) for mark in QUESTION_MARKS)


def is_command_text(text: str, markers: tuple[str, ...] = DEFAULT_COMMAND_MARKERS) -> bool:
    stripped = (text or "").lstrip()
    if not stripped:
        return False
    return any(stripped.startswith(marker) for marker in markers if marker)


def evaluate(gate: GateInput) -> GateDecision:
    """按固定顺序过闸；返回第一个拒绝原因。"""
    if not gate.auto_send_enabled:
        return GateDecision(False, Reason.SWITCH_OFF)
    if gate.turn_used:
        return GateDecision(False, Reason.TURN_USED)
    if gate.turn_cancelled:
        return GateDecision(False, Reason.TURN_SUPERSEDED)
    if gate.reply_has_image:
        return GateDecision(False, Reason.REPLY_HAS_IMAGE)
    if not gate.session_allowed:
        return GateDecision(False, Reason.SESSION_DENIED)
    if gate.session_cooling:
        return GateDecision(False, Reason.SESSION_COOLDOWN)
    if visible_length(gate.text) <= 2:
        return GateDecision(False, Reason.TEXT_TOO_SHORT, f"len={visible_length(gate.text)}")
    if is_command_text(gate.text, gate.command_markers):
        return GateDecision(False, Reason.TEXT_IS_COMMAND)
    if question_count(gate.text) >= 2:
        return GateDecision(False, Reason.TEXT_TOO_MANY_QUESTIONS)
    if gate.roll >= gate.ratio:
        return GateDecision(False, Reason.DICE_LOST, f"roll={gate.roll:.3f}")
    if not gate.has_candidate:
        return GateDecision(False, Reason.NO_CANDIDATE)
    return _ALLOWED


__all__ = [
    "GateDecision",
    "GateInput",
    "evaluate",
    "is_command_text",
    "question_count",
    "reply_contains_image",
    "reply_text",
    "visible_length",
]

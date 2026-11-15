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

DEFAULT_COMMAND_MARKERS: tuple[str, ...] = ("/", "!", "#", "！", "／")
"""回复文本的兜底前缀。句号不在里面：中文回复以「。」开头很常见，
拿它当命令标记会把正常回复当成指令输出，静默跳过一次配图。"""
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

_MODEL_RESULT_KINDS = frozenset({"LLM_RESULT", "AGENT_RUNNER_ERROR", "STREAMING_FINISH"})
"""宿主 `ResultContentType` 里属于「模型在说话」的那几种；其余（GENERAL_RESULT）
是命令回复或其他插件主动发的文本。按名字比对，免得为读一个枚举去 import 宿主 core。"""


def result_is_model_reply(result: Any) -> bool:
    """这条结果是不是模型自己生成的回复（含流式结束）。

    命令回复、别的插件主动发的文本同样是「机器人说了句话」，但那是指令输出，
    补一张表情只会显得在管理操作里乱插话。类型是准的，文本前缀只能兜底。
    """
    if result is None:
        # on_llm_response 阶段结果还没设上，而这条文本本来就来自模型
        return True
    checker = getattr(result, "is_model_result", None)
    if callable(checker):
        try:
            if checker():
                return True
        except Exception:
            # 宿主提供的判定不可用就退到下面按内容类型判断：这一步是探测，
            # 失败不等于「不是模型回复」，不能据此直接拒绝。
            pass
    kind = getattr(getattr(result, "result_content_type", None), "name", "")
    return kind in _MODEL_RESULT_KINDS


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


# 发送延迟拟人化：固定 5 秒在群里看久了能被感觉到是机器人。
# 实际延迟 = 基础值 × 抖动系数 + 文本长度因子（长回复像"打了更多字"）。
DELAY_JITTER_LOW = 0.75
DELAY_JITTER_HIGH = 1.25
DELAY_PER_CHARS = 30.0  # 每这么多可见字符多等一秒
DELAY_TEXT_CAP = 2.0  # 文本因子的上限（秒）


def humanized_delay(base_seconds: float, text: str, roll: float) -> float:
    """围绕配置值抖动的发送延迟；base 为 0 表示用户要立即发，保持 0。"""
    base = max(0.0, float(base_seconds))
    if base <= 0:
        return 0.0
    jitter = DELAY_JITTER_LOW + (DELAY_JITTER_HIGH - DELAY_JITTER_LOW) * min(1.0, max(0.0, roll))
    text_extra = min(DELAY_TEXT_CAP, visible_length(text) / DELAY_PER_CHARS)
    return base * jitter + text_extra


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
    "result_is_model_reply",
    "visible_length",
]

"""发送侧与宿主框架的唯一接触面（F4.3 / F4.6）。

只做「把本地文件变成一条独立消息」这一件事：不改原回复、不重试。

失败不抛给调用方（发送是回合内的一次性动作，抛出去只会打断整条回复），
但必须**带错因返回**：调用方据此计「发送失败」、把异常类型写进日志与发送历史，
否则平台侧的报错会彻底消失（2026-11-15 按错误处理底线整改）。

astrbot 的两个组件**延迟导入**：这样 `deliver` 的失败分类逻辑能脱离宿主跑测试
（测试环境不允许 import 真实宿主模块）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - 仅类型标注
    from astrbot.api.event import MessageChain

logger = logging.getLogger(__name__)


def build_chain(path: Path) -> MessageChain:
    from astrbot.api.event import MessageChain
    from astrbot.api.message_components import Image

    return MessageChain(chain=[Image.fromFileSystem(str(path))])


async def deliver(context: Any, umo: str, path: Path) -> tuple[bool, str]:
    """作为独立消息发出，返回 (是否成功, 失败原因)。

    失败原因只放**异常类型名**或 `platform_unavailable`，不放聊天内容——
    发送历史要能回答「为什么没发出去」，而日志里同样只留类型。
    """
    try:
        chain = build_chain(path)
        ok = bool(await context.send_message(umo, chain))
    except Exception as exc:
        # 只记异常类型，不带 umo/路径以外的内容；平台报错原样保留在 cause 里
        logger.warning("表情投递失败 type=%s", type(exc).__name__)
        return False, type(exc).__name__
    if not ok:
        # 宿主返回 False = 找不到对应平台/会话，不是异常，单独给个原因
        return False, "platform_unavailable"
    return True, ""


__all__ = ["build_chain", "deliver"]

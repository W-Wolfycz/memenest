"""发送侧与宿主框架的唯一接触面（F4.3 / F4.6）。

只做「把本地文件变成一条独立消息」这一件事：不改原回复、不重试。

失败不抛给调用方（发送是回合内的一次性动作，抛出去只会打断整条回复），
但必须**带错因返回 False**：调用方据此计「发送失败」并把异常类型写进日志，
否则平台侧的报错会彻底消失（2026-11-15 按错误处理底线整改）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image

logger = logging.getLogger(__name__)


def build_chain(path: Path) -> MessageChain:
    return MessageChain(chain=[Image.fromFileSystem(str(path))])


async def deliver(context: Any, umo: str, path: Path) -> bool:
    """作为独立消息发出；失败返回 False，并在这里留下 WARNING（含异常类型）。"""
    try:
        chain = build_chain(path)
        return bool(await context.send_message(umo, chain))
    except Exception as exc:
        # 只记异常类型，不带 umo/路径以外的内容；平台报错原样保留在 cause 里
        logger.warning("表情投递失败 type=%s", type(exc).__name__)
        return False


__all__ = ["build_chain", "deliver"]

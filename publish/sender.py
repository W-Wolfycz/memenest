"""发送侧与宿主框架的唯一接触面（F4.3 / F4.6）。

只做「把本地文件变成一条独立消息」这一件事：不改原回复、不重试、失败静默。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from astrbot.api.event import MessageChain
from astrbot.api.message_components import Image


def build_chain(path: Path) -> MessageChain:
    return MessageChain(chain=[Image.fromFileSystem(str(path))])


async def deliver(context: Any, umo: str, path: Path) -> bool:
    """作为独立消息发出；任何异常都吞掉并返回 False（只记 DEBUG 由调用方做）。"""
    try:
        chain = build_chain(path)
        return bool(await context.send_message(umo, chain))
    except Exception:
        return False


__all__ = ["build_chain", "deliver"]

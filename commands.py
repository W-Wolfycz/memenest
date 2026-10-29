"""管理指令的执行层（F5）：把 ParsedCommand 变成一次库操作 + 一句回复。

只依赖注入进来的 plugin 门面，不 import 宿主框架——所以可以脱离 AstrBot 单测。
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import commands_core as core
from .ingest.collect import quoted_image_sources

class CommandRunner:
    def __init__(self, plugin: Any) -> None:
        self.plugin = plugin

    # -- 门面 -------------------------------------------------------------

    @property
    def store(self) -> Any:
        return self.plugin.store

    @property
    def data_dir(self) -> Path:
        return self.plugin.data_dir

    async def run(self, event: Any, parsed: core.ParsedCommand) -> str:
        umo = getattr(event, "unified_msg_origin", "")
        if parsed.is_write and not _is_admin(event):
            return "这条指令需要管理员权限。"
        handler = getattr(self, f"_do_{parsed.sub}", None)
        if handler is None:
            return f"没有这个子命令。\n{core.render_help(self.plugin.command_name())}"
        try:
            return await handler(event, parsed, umo)
        except Exception as exc:  # 指令失败不掀翻消息处理
            self.plugin.log_error("指令 %s 执行失败 type=%s", parsed.sub, type(exc).__name__)
            return f"指令执行失败（{type(exc).__name__}）。"

    # -- 读操作 -----------------------------------------------------------

    async def _do_help(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        return core.render_help(self.plugin.command_name())

    async def _do_unknown(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        return f"没有这个子命令。\n{core.render_help(self.plugin.command_name())}"

    async def _do_status(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        stats = await asyncio.to_thread(self.store.stats)
        return core.render_status(
            stats,
            collect_enabled=self.plugin.collect_enabled(),
            library_cap=self.plugin.library_cap(),
            queue_depth=self.plugin.queue_depth(),
            vector=self.plugin.vector_state(),
            model_available=self.plugin.model_available(),
        )

    # -- 写操作 -----------------------------------------------------------

    async def _do_on(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        minutes = core.parse_switch_minutes(parsed.args[0] if parsed.args else None)
        if not self.plugin.set_collect(True, minutes=minutes):
            return ""  # 本来就开着，静默
        if minutes:
            return f"收集已开启，{minutes} 分钟后自动恢复"
        return "收集已开启（本次运行有效）"

    async def _do_off(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        minutes = core.parse_switch_minutes(parsed.args[0] if parsed.args else None)
        if not self.plugin.set_collect(False, minutes=minutes):
            return ""  # 本来就关着，静默
        if minutes:
            return f"收集已关闭，{minutes} 分钟后自动恢复"
        return "收集已关闭（已排队的任务会跑完）"

    async def _do_grab(self, event: Any, parsed: core.ParsedCommand, umo: str) -> str:
        sources = quoted_image_sources(event)
        if not sources:
            return "要引用一条带图片的消息才能收"
        queued, dropped = await self.plugin.grab_quoted(sources, umo)
        if not queued:
            return "没收到，可能是队列满了，稍后再试"
        return f"已收录 {queued} 张" + (f"（{dropped} 张因队列已满被丢弃）" if dropped else "")


def _is_admin(event: Any) -> bool:
    checker = getattr(event, "is_admin", None)
    if callable(checker):
        try:
            return bool(checker())
        except Exception:
            return False
    return getattr(event, "role", "") == "admin"

__all__ = ["CommandRunner"]

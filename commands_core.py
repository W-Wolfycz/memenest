"""管理指令的纯逻辑（F5）：解析、分页、文案渲染。不含任何宿主框架 import。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


PAGE_SIZE_DEFAULT = 10
READ_SUBS: frozenset[str] = frozenset({"status", "help"})
WRITE_SUBS: frozenset[str] = frozenset({"on", "off", "grab"})
ALL_SUBS = READ_SUBS | WRITE_SUBS
LEADING_MARKS = ("/", "!", "！", "／", "#", ".")
_MENTION = re.compile(r"^(?:@\S+\s+)+")


@dataclass(slots=True)
class ParsedCommand:
    sub: str
    args: tuple[str, ...] = ()
    raw_args: str = ""

    @property
    def is_write(self) -> bool:
        return self.sub in WRITE_SUBS


def parse_command(text: str, command_name: str) -> ParsedCommand | None:
    """从完整消息文本里解析子命令；不是本插件指令时返回 None。

    要求指令名后面是空白或结尾，避免「mn 开头的普通句子」被当成指令。
    """
    name = (command_name or "mn").strip()
    if not name:
        return None
    cleaned = _MENTION.sub("", (text or "").strip())
    for mark in LEADING_MARKS:
        if cleaned.startswith(mark):
            cleaned = cleaned[len(mark) :].lstrip()
            break
    pattern = re.compile(rf"^{re.escape(name)}(?:\s+(.*))?$", re.IGNORECASE | re.DOTALL)
    match = pattern.match(cleaned)
    if match is None:
        return None
    rest = (match.group(1) or "").strip()
    if not rest:
        return ParsedCommand("status")
    parts = rest.split()
    sub = parts[0].lower()
    if sub not in ALL_SUBS:
        return ParsedCommand("unknown", tuple(parts), rest)
    args = tuple(parts[1:])
    return ParsedCommand(sub, args, " ".join(parts[1:]))


def render_help(command_name: str) -> str:
    return "\n".join(
        [
            f"表情窝（/{command_name}）：",
            "  grab          引用一条带图的消息，把那张图收进来",
            "  on [分钟]     临时开启收集，不给分钟数就本次运行有效",
            "  off [分钟]    临时关闭收集，已排队的任务会跑完",
            "  status        看库存与运行状态",
        ]
    )


def render_status(
    stats: Any,
    *,
    collect_enabled: bool,
    library_cap: int,
    queue_depth: int,
    vector: dict[str, Any] | None = None,
    model_available: bool = True,
) -> str:
    """库存与运行状态，按面板式排版。

    只放「一眼要看到」的数字：总量、三种状态、黑名单、向量覆盖、识别模型。
    识别模型单独一行：没配的时候新条目会全部落成「其他」，不说出来没人知道为什么。
    """
    total = stats.total
    vector = vector or {}
    covered = int(vector.get("covered") or 0)
    expected = int(vector.get("expected") or total)
    if vector.get("enabled"):
        vector_text = f"{covered}/{expected}"
    else:
        vector_text = "未启用"

    PAD = "\u3000"  # 全角空格，用来对齐中文名称
    head = "🖼️ 表情窝" + ("" if collect_enabled else " · 收集已暂停")
    rows = [
        ("📦", "总数", str(total)),
        ("📥", "待审核", str(stats.inbox)),
        ("✅", "可用", str(stats.ready)),
        ("⏸️", "已禁用", str(stats.disabled)),
        ("🚫", "黑名单", str(stats.blocked)),
        ("🧠", "向量", vector_text),
        ("🔍", "识别", "可用" if model_available else "未配置"),
    ]
    body = "\n".join(
        f"{icon} {name}{PAD * (4 - len(name))}{value:>5}"
        for icon, name, value in rows
    )
    tail = f"队列 {queue_depth} · 容量 {library_cap}"
    return f"{head}\n{'─' * 20}\n{body}\n{'─' * 20}\n{tail}"


def parse_switch_minutes(
    token: str | None, minimum: int = 1, maximum: int = 24 * 60
) -> int | None:
    """on/off 的时长参数：给不出合法分钟数就返回 None（表示不限时）。"""
    if token is None or not str(token).strip():
        return None
    value = parse_int(token)
    if value is None or value <= 0:
        return None
    return max(minimum, min(maximum, value))


def parse_int(token: str) -> int | None:
    try:
        return int(str(token).strip())
    except (TypeError, ValueError):
        return None


__all__ = [
    "ALL_SUBS",
    "ParsedCommand",
    "READ_SUBS",
    "WRITE_SUBS",
    "parse_command",
    "parse_switch_minutes",
    "render_help",
    "render_status",
]

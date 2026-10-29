"""挑选与发送：闸 → 打分 → 调度 → 发送。

sender 会被惰性加载：它 import 宿主框架，纯逻辑单测不需要把它拖进来。
"""

from __future__ import annotations

import importlib
from typing import Any

from . import dispatcher, gate, pick

__all__ = ["dispatcher", "gate", "pick", "sender"]


def __getattr__(name: str) -> Any:
    """按需导入 sender。

    不能用 ``from . import sender``：那会回到 fromlist 的 getattr 路径，
    在子模块尚未注入包命名空间时形成递归。
    """
    if name == "sender":
        module = importlib.import_module(".sender", __name__)
        globals()["sender"] = module
        return module
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

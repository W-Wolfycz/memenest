"""表情窝（memenest）插件包。

入口是 ``main.py``（AstrBot 扫描器只认 ``main.py`` 或 ``<目录名>.py``），主类 ``Main``
定义在 ``main.py`` 里——这样 ``star_map`` 的键才会是 ``data.plugins.memenest.main``，
与加载器写入的 module_path 一致，handler 才能正确绑定插件实例。

这里仍走惰性导入：纯逻辑子模块（domain / store / ingest / publish）可以在没有宿主框架的
环境里被单独导入做单测，不会被 astrbot 依赖拖累。
"""

from __future__ import annotations

from typing import Any

__all__ = ["Main"]


def __getattr__(name: str) -> Any:
    if name == "Main":
        from .main import Main

        return Main
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

"""T-25 / T-26 / T-28 / F6：工具编号生命周期、原因码协议、指令解析与配置钳制。

守的静默失效：过期的候选编号仍然能发出表情（跨回合误发）、
原因码被悄悄改掉（上游按字符串分流的地方全部失效）、
配置越界值没有钳制（0 容量、负数延迟让运行期行为失控）。
"""

from __future__ import annotations

from _fixtures import make_memo
from memenest import tools_core


def test_T26_stale_handles_are_rejected_after_invalidation():
    """回合失效后编号必须作废：否则用户以为换了话题，LLM 却还发着上一轮选中的图。"""
    registry = tools_core.HandleRegistry()
    registry.issue("umo:1", [make_memo(1)])
    assert registry.has_session("umo:1") is True
    registry.invalidate("umo:1")
    assert registry.has_session("umo:1") is False
    assert registry.resolve("umo:1", "1") is None

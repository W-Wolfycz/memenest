"""投递结果的失败分类：发送历史要能说出「为什么没发出去」。

守的静默失效：投递失败一律记成「失败」而丢掉异常类型——历史里只剩两个字的
「失败」，排查时还是得回去翻日志对时间；宿主返回 False 与抛异常也给混成一种。
"""

from __future__ import annotations

import pytest

from memenest.publish import sender


class _CtxRaises:
    async def send_message(self, umo: str, chain: object) -> bool:
        raise TimeoutError("boom")


class _CtxFalse:
    async def send_message(self, umo: str, chain: object) -> bool:
        return False


class _CtxOk:
    async def send_message(self, umo: str, chain: object) -> bool:
        return True


@pytest.mark.asyncio
async def test_deliver_reports_error_type(tmp_path, monkeypatch):
    monkeypatch.setattr(sender, "build_chain", lambda path: object())
    target = tmp_path / "a.png"
    target.write_bytes(b"x")

    ok, detail = await sender.deliver(_CtxRaises(), "group_demo", target)
    assert ok is False
    assert detail == "TimeoutError", "失败原因要落到异常类型名"

    ok, detail = await sender.deliver(_CtxFalse(), "group_demo", target)
    assert ok is False
    assert detail == "platform_unavailable", "宿主返回 False 是另一类原因"

    ok, detail = await sender.deliver(_CtxOk(), "group_demo", target)
    assert ok is True
    assert detail == ""

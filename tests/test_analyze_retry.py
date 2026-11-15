"""识图重试策略（`analyze_with_retry`）。

守的静默失效：第一次失败就直接放弃 → 网络抖一下这张图就没了（不入库、不报错，
用户只觉得「最近收得少」）；重试次数写成无限或忽略预算 → 刷图天费用失控。
"""

from __future__ import annotations

import pytest
from PIL import Image

from memenest.ingest import analyze as analyze_mod
from memenest.ingest.analyze import analyze_with_retry


def _write_png(path) -> None:
    Image.new("RGB", (24, 24), (30, 120, 200)).save(path, format="PNG")


class _Flaky:
    """前 fail_times 次抛异常，之后返回合法 JSON。"""

    def __init__(self, fail_times: int) -> None:
        self.fail_times = fail_times
        self.calls = 0

    async def __call__(self, prompt: str, images: list[str]) -> str:
        self.calls += 1
        if self.calls <= self.fail_times:
            raise RuntimeError("boom")
        return '{"label": "laugh", "caption": "猫在笑"}'


@pytest.mark.asyncio
async def test_retry_recovers_from_one_transient_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze_mod, "VISION_RETRY_BACKOFF", 0.0)
    source = tmp_path / "a.png"
    _write_png(source)
    call = _Flaky(fail_times=1)
    seen: list[int] = []

    outcome = await analyze_with_retry(
        source,
        prompt="p",
        llm_call=call,
        work_dir=tmp_path,
        on_attempt=lambda: seen.append(1),
    )

    assert outcome.analysis is not None, "第一次失败后应当重试成功"
    assert outcome.analysis.caption == "猫在笑"
    assert call.calls == 2
    assert len(seen) == 2, "每次真调用都要计一次预算"


@pytest.mark.asyncio
async def test_budget_exhausted_stops_before_calling(tmp_path):
    """预算用尽时一次都不能调：否则「每日上限」形同虚设。"""
    source = tmp_path / "b.png"
    _write_png(source)
    call = _Flaky(fail_times=0)

    outcome = await analyze_with_retry(
        source,
        prompt="p",
        llm_call=call,
        work_dir=tmp_path,
        can_attempt=lambda: False,
    )

    assert outcome.analysis is None
    assert call.calls == 0, "预算没余量时不许发起调用"


@pytest.mark.asyncio
async def test_attempts_cap_is_respected(tmp_path, monkeypatch):
    monkeypatch.setattr(analyze_mod, "VISION_RETRY_BACKOFF", 0.0)
    source = tmp_path / "c.png"
    _write_png(source)
    call = _Flaky(fail_times=99)

    outcome = await analyze_with_retry(
        source, prompt="p", llm_call=call, work_dir=tmp_path, attempts=2
    )

    assert outcome.analysis is None
    assert call.calls == 2, "重试次数必须封顶，不能变成循环重试"
    assert outcome.reason.startswith("model_error")

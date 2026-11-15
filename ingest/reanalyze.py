"""重新识图：对已有条目重跑视觉模型，把描述/标签/场景补齐。

用途：识图在某个时段失效时（provider 未就绪、预算用尽、模型报错），条目会以
兜底类入库且没有描述——这些条目检索不到、也看不出内容，只能重跑识别才算救回来。
删掉是最后手段，所以先给「重新识图」。

与入库走同一条 `analyze_with_retry`，所以重试策略、预算计数、异常日志都一致。
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .analyze import LLMCall, analyze_with_retry

MAX_BATCH = 2000
"""一次重新识图最多处理多少条：再多就该分几次，别把整条流水线占住。"""

SCOPE_EMPTY = "empty"
SCOPE_IDS = "ids"


@dataclass
class ReanalyzeState:
    running: bool = False
    done: int = 0
    total: int = 0
    filled: int = 0
    failed: int = 0
    message: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "done": self.done,
            "total": self.total,
            "filled": self.filled,
            "failed": self.failed,
            "message": self.message,
        }


class Reanalyzer:
    """串行重跑识别；同一时刻只允许一个任务（和「补齐」一样的占位语义）。"""

    def __init__(
        self,
        *,
        store: Any,
        data_dir: Path,
        llm_call: Callable[[], LLMCall | None],
        prompt: str,
        log: Any,
        can_attempt: Callable[[], bool] | None = None,
        on_attempt: Callable[[], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.store = store
        self.data_dir = Path(data_dir)
        self._llm_call = llm_call
        self._prompt = prompt
        self._log = log
        self._can_attempt = can_attempt
        self._on_attempt = on_attempt
        self._clock = clock
        self.state = ReanalyzeState()

    # -- 占位 -------------------------------------------------------------

    def try_begin(self) -> bool:
        if self.state.running:
            return False
        self.state = ReanalyzeState(running=True)
        return True

    def targets(self, scope: str, ids: list[int] | None = None) -> list[int]:
        """待处理条目：`empty` = 所有无描述的，`ids` = 指定 id（上限 MAX_BATCH）。"""
        if scope == SCOPE_IDS:
            picked = [int(item) for item in (ids or [])][:MAX_BATCH]
            return picked
        return self.store.ids_without_description(MAX_BATCH)

    # -- 执行 -------------------------------------------------------------

    async def run(self, target_ids: list[int]) -> dict[str, Any]:
        call = self._llm_call()
        if call is None:
            self.state = ReanalyzeState(
                message="没有可用的视觉模型，重新识图已跳过"
            )
            return {"ok": False, "reason": "no_model", **self.state.snapshot()}

        self.state.total = len(target_ids)
        for memo_id in target_ids:
            memo = await asyncio.to_thread(self.store.get, memo_id)
            if memo is None:
                self.state.done += 1
                continue
            source = self.data_dir / memo.rel_path
            if not source.is_file():
                # 文件不在磁盘上：这不是识图能修的，计入失败让用户去体检里看
                self.state.failed += 1
                self.state.done += 1
                continue
            outcome = await analyze_with_retry(
                source,
                prompt=self._prompt,
                llm_call=call,
                work_dir=self.data_dir / "tmp",
                can_attempt=self._can_attempt,
                on_attempt=self._on_attempt,
            )
            if outcome.analysis is None:
                self.state.failed += 1
                self.state.done += 1
                self._log.debug(
                    "[MemeNest] 重新识图失败 id=%s reason=%s", memo_id, outcome.reason
                )
                if self._can_attempt is not None and not self._can_attempt():
                    self.state.message = "识图预算用尽，已中止"
                    break
                continue
            analysis = outcome.analysis
            await asyncio.to_thread(
                self.store.update_analysis,
                memo_id,
                caption=analysis.caption,
                text_in_image=analysis.text_in_image,
                scenes=list(analysis.scenes),
                labels=list(analysis.labels),
                label=analysis.label,
            )
            self.state.filled += 1
            self.state.done += 1
            await asyncio.sleep(0)  # 让出控制权，别卡消息处理
        return {"ok": True, **self.state.snapshot()}

    async def run_and_release(self, target_ids: list[int]) -> dict[str, Any]:
        """占位后的执行入口：无论成功失败都把 running 放回 False。"""
        try:
            result = await self.run(target_ids)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._log.warning(
                "[MemeNest] 重新识图中止 type=%s", type(exc).__name__
            )
            result = {
                "ok": False,
                "reason": type(exc).__name__,
                "message": f"重新识图中止（{type(exc).__name__}）",
            }
        finally:
            self.state.running = False
        if not result.get("message"):
            result["message"] = (
                f"完成：更新 {self.state.filled} 条，失败 {self.state.failed} 条"
            )
        self.state.message = str(result["message"])
        self._log.info(
            "[MemeNest] 重新识图结束 total=%s filled=%s failed=%s",
            self.state.total,
            self.state.filled,
            self.state.failed,
        )
        return result


__all__ = [
    "MAX_BATCH",
    "SCOPE_EMPTY",
    "SCOPE_IDS",
    "ReanalyzeState",
    "Reanalyzer",
]

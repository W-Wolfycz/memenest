"""收集节流（R-2）。

顺序不变量：调用方必须先做 R-1 判定，只有合格的媒体才允许消耗冷却窗口。
本模块只持有「上次放行时间」，不做任何判定之外的副作用。
"""

from __future__ import annotations

import random
from dataclasses import dataclass


@dataclass(slots=True)
class Throttle:
    mode: str = "ratio"
    ratio: float = 0.3
    cooldown_seconds: int = 30
    last_grant: float = 0.0

    def allows(self, now: float, roll: float | None = None) -> tuple[bool, str]:
        """返回 (是否放行, 原因)。放行时才推进冷却窗口。"""
        if self.mode == "cooldown":
            if self.cooldown_seconds <= 0:
                self.last_grant = now
                return True, "throttle_cooldown_off"
            if self.last_grant <= 0 or now - self.last_grant >= self.cooldown_seconds:
                self.last_grant = now
                return True, "throttle_cooldown_pass"
            return False, "throttle_cooldown_wait"
        value = random.random() if roll is None else roll
        if value < self.ratio:
            self.last_grant = now
            return True, "throttle_ratio_pass"
        return False, "throttle_ratio_miss"


__all__ = ["Throttle"]

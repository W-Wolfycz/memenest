"""T-03 / T-04 / R-2：收集节流。

守的静默失效：把节流放到判定之前，普通照片把冷却窗口白白吃掉，
群聊一旦刷图，真表情包就再也收不到了。
"""

from __future__ import annotations


from memenest.ingest.collect import (
    COLLECT,
    SKIP_BUDGET,
    SKIP_INBOX_FULL,
    SKIP_NO_MEDIA,
    SKIP_THROTTLED,
    plan_collect,
)
from memenest.ingest.throttle import Throttle


def test_plan_collect_does_not_consume_throttle_for_unqualified_media():
    """先判合格再判节流，且被拒绝时不推进窗口。

    普通照片把冷却窗口吃掉、或者「被拒绝也算数」，都会让真表情包再也收不到；
    这两种错误都只表现为「最近怎么不收了」。
    """
    throttle = Throttle(mode="cooldown", cooldown_seconds=30)
    plan = plan_collect(enabled=True, has_refs=False, throttle=throttle, now=100.0)
    assert plan.action == SKIP_NO_MEDIA
    assert throttle.last_grant == 0.0

    plan = plan_collect(enabled=True, has_refs=True, throttle=throttle, now=100.0)
    assert plan.action == COLLECT
    assert throttle.last_grant == 100.0

    plan = plan_collect(enabled=True, has_refs=True, throttle=throttle, now=110.0)
    assert plan.action == SKIP_THROTTLED
    # 被拒绝的时刻不能推进窗口，否则冷却永远不到期
    assert throttle.last_grant == 100.0

    plan = plan_collect(enabled=True, has_refs=True, throttle=throttle, now=130.0)
    assert plan.action == COLLECT
    assert throttle.last_grant == 130.0

    # 待审核池满：跳过收集，而且不消耗冷却窗口（满了还掷骰子只会白耗）
    plan = plan_collect(
        enabled=True, has_refs=True, throttle=throttle, now=200.0, inbox_full=True
    )
    assert plan.action == SKIP_INBOX_FULL
    assert throttle.last_grant == 130.0

    # 识图预算用尽：同样跳过且不消耗窗口
    plan = plan_collect(
        enabled=True, has_refs=True, throttle=throttle, now=210.0, budget_exhausted=True
    )
    assert plan.action == SKIP_BUDGET
    assert throttle.last_grant == 130.0

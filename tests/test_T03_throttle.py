"""T-03 / T-04 / R-2：收集节流。

守的静默失效：把节流放到判定之前，普通照片把冷却窗口白白吃掉，
群聊一旦刷图，真表情包就再也收不到了。
"""

from __future__ import annotations


from memenest.ingest.throttle import Throttle


def test_cooldown_blocks_then_allows_and_only_grant_advances_window():
    """只有放行才推进冷却窗口：被拒绝的也算数会让冷却永不到期，表情再也发不出来。"""
    throttle = Throttle(mode="cooldown", cooldown_seconds=30)
    allowed, reason = throttle.allows(1000.0)
    assert (allowed, reason) == (True, "throttle_cooldown_pass")
    allowed, reason = throttle.allows(1010.0)
    assert (allowed, reason) == (False, "throttle_cooldown_wait")
    assert throttle.last_grant == 1000.0
    allowed, _ = throttle.allows(1030.0)
    assert allowed is True
    assert throttle.last_grant == 1030.0
def test_plan_collect_does_not_consume_throttle_for_unqualified_media():
    """先判合格再判节流：普通照片不得推进冷却窗口。"""
    from memenest.ingest.collect import COLLECT, SKIP_NO_MEDIA, SKIP_THROTTLED, plan_collect
    from memenest.ingest.throttle import Throttle

    throttle = Throttle(mode="cooldown", cooldown_seconds=30)
    plan = plan_collect(
        enabled=True, has_refs=False, throttle=throttle, now=100.0
    )
    assert plan.action == SKIP_NO_MEDIA
    assert throttle.last_grant == 0.0

    plan = plan_collect(
        enabled=True, has_refs=True, throttle=throttle, now=100.0
    )
    assert plan.action == COLLECT
    assert throttle.last_grant == 100.0

    plan = plan_collect(
        enabled=True, has_refs=True, throttle=throttle, now=110.0
    )
    assert plan.action == SKIP_THROTTLED

"""候选集缓存：把「查库」压成「查一次内存」。

加速手段只有三条，都不改变检索语义：
1. 命中缓存的回合不再查库（写操作会推进 store.epoch，缓存立刻作废）；
2. 预载时忽略作用域，作用域在内存里按会话过滤（同一个池子服务所有会话）；
3. 人格过滤下推到 SQL，避免把其他人格的表情读进来。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from ..config import Settings
from ..domain import STATE_READY, CandidateQuery, Memo
from ..store import MemoStore

PRELOAD_LIMIT = 2000
CACHE_TTL_SECONDS = 30
CACHE_ENTRIES = 8


@dataclass(slots=True)
class _Entry:
    items: list[Memo]
    epoch: int
    loaded_at: float


class CandidateCache:
    def __init__(
        self,
        store: MemoStore,
        settings: Callable[[], Settings],
        clock: Callable[[], float] = time.monotonic,
        log: Any = None,
    ) -> None:
        self.store = store
        self._settings = settings
        self._clock = clock
        self._log = log
        self._entries: dict[tuple[str | None, str | None], _Entry] = {}
        self._lock = asyncio.Lock()
        self.hits = 0
        self.misses = 0

    def invalidate(self) -> None:
        self._entries.clear()

    async def get(
        self, *, persona_id: str | None, label: str | None = None
    ) -> list[Memo]:
        settings = self._settings()
        ttl = CACHE_TTL_SECONDS
        persona_key = persona_id if settings.persona_isolation else None
        key = (persona_key, label)
        epoch = self.store.epoch
        now = self._clock()
        entry = self._entries.get(key)
        if entry is not None and entry.epoch == epoch and (ttl <= 0 or now - entry.loaded_at < ttl):
            self.hits += 1
            return entry.items

        async with self._lock:
            entry = self._entries.get(key)
            epoch = self.store.epoch
            now = self._clock()
            if entry is not None and entry.epoch == epoch and (
                ttl <= 0 or now - entry.loaded_at < ttl
            ):
                self.hits += 1
                return entry.items
            query = CandidateQuery(
                state=STATE_READY,
                label=label,
                umo=None,
                ignore_scope=True,
                limit=PRELOAD_LIMIT,
                persona_id=persona_key,
                persona_filter=settings.persona_isolation,
            )
            items = await asyncio.to_thread(self.store.candidates, query)
            self.misses += 1
            if len(self._entries) >= CACHE_ENTRIES:
                self._entries.clear()
            self._entries[key] = _Entry(items=items, epoch=epoch, loaded_at=now)
            if self._log is not None:
                self._log.debug(
                    "memenest: 候选缓存加载 persona=%s label=%s count=%s",
                    persona_key,
                    label,
                    len(items),
                )
            return items

    def stats(self) -> dict[str, int]:
        return {
            "缓存命中": self.hits,
            "缓存未命中": self.misses,
            "缓存条目": len(self._entries),
        }


__all__ = ["CandidateCache", "PRELOAD_LIMIT"]

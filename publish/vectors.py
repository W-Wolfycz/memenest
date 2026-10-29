"""向量索引缓存：与候选缓存同理，把「查库 + 解包」压成一次内存读取。"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from typing import Any

from .. import embed as emb
from ..store import MemoStore

CACHE_TTL_SECONDS = 30


class VectorStore:
    """按模型名缓存解包后的归一化向量。

    失效条件与候选缓存一致：store.epoch 推进（任何写操作）或 TTL 到期。
    """

    def __init__(
        self,
        store: MemoStore,
        clock: Callable[[], float] = time.monotonic,
        log: Any = None,
    ) -> None:
        self.store = store
        self._clock = clock
        self._log = log
        self._model = ""
        self._dim = 0
        self._epoch = -1
        self._loaded_at = 0.0
        self._vectors: dict[int, dict[str, tuple[float, ...]]] = {}
        self._lock = asyncio.Lock()

    def invalidate(self) -> None:
        self._epoch = -1

    async def get(self, model: str, dim: int) -> dict[int, dict[str, tuple[float, ...]]]:
        if not model:
            return {}
        epoch = self.store.epoch
        now = self._clock()
        fresh = (
            self._model == model
            and self._dim == dim
            and self._epoch == epoch
            and now - self._loaded_at < CACHE_TTL_SECONDS
        )
        if fresh:
            return self._vectors

        async with self._lock:
            epoch = self.store.epoch
            now = self._clock()
            if (
                self._model == model
                and self._dim == dim
                and self._epoch == epoch
                and now - self._loaded_at < CACHE_TTL_SECONDS
            ):
                return self._vectors
            rows = await asyncio.to_thread(self.store.load_embeddings, model)
            vectors: dict[int, dict[str, tuple[float, ...]]] = {}
            for memo_id, by_kind in rows.items():
                kept: dict[str, tuple[float, ...]] = {}
                for kind, (row_dim, blob) in by_kind.items():
                    if row_dim != dim:
                        continue  # 维度不符（换过模型）当没有，等后台重建
                    packed = emb.unpack(blob, dim)
                    if packed:
                        kept[kind] = packed
                if kept:
                    vectors[memo_id] = kept
            self._model = model
            self._dim = dim
            self._epoch = epoch
            self._loaded_at = now
            self._vectors = vectors
            if self._log is not None:
                self._log.debug(
                    "memenest: 向量索引加载 model=%s dim=%s 条目=%s 行=%s",
                    model,
                    dim,
                    len(vectors),
                    sum(len(kinds) for kinds in vectors.values()),
                )
            return self._vectors

__all__ = ["VectorStore"]

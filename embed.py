"""向量检索的底层设施：打包、余弦、参与向量的文本、provider 包装。

刻意不依赖 numpy：500 条量级的点积用纯 Python 只要十几毫秒，不值得为此加依赖。
入库与查询两侧都先归一化，检索时点积即余弦。
"""

from __future__ import annotations

import asyncio
import math
from array import array
from dataclasses import dataclass
from typing import Any, Iterable


# 维度只在 provider 拿不到时使用；正常路径永远以 provider.get_dim() 为准
FALLBACK_DIM = 768


def pack(vec: Iterable[float]) -> bytes:
    return array("f", vec).tobytes()


def unpack(blob: bytes, dim: int) -> tuple[float, ...]:
    """维度对不上就当没有：宁可当空向量，也不拿错位数据参与排序。"""
    data = array("f")
    try:
        data.frombytes(blob)
    except ValueError:
        return ()
    if len(data) != dim:
        return ()
    return tuple(data)


def normalize(vec: Iterable[float]) -> tuple[float, ...]:
    values = tuple(float(item) for item in vec)
    norm = math.sqrt(sum(value * value for value in values))
    if not norm:
        return values
    return tuple(value / norm for value in values)


def dot(a: tuple[float, ...], b: tuple[float, ...]) -> float:
    """两侧都已归一化时，点积就是余弦相似度。"""
    if not a or len(a) != len(b):
        return 0.0
    return sum(x * y for x, y in zip(a, b))


KIND_TEXT = "text"
KIND_CAPTION = "caption"
KIND_SCENE = "scene"
KINDS = (KIND_TEXT, KIND_CAPTION, KIND_SCENE)


def vectors_for(
    *,
    text_in_image: str,
    caption: str,
    scenes: tuple[str, ...] | list[str],
) -> dict[str, str]:
    """三路各自的向量文本；空串表示这一路没有内容，不建向量。

    分开而不是拼成一个字符串，是因为三路的语义作用完全不同：
    - text  : 图上印的字，对应「机器人要说的这句话」
    - caption: 画面描述，对应「机器人在说这类内容」
    - scene : 话题词，对应「聊到这个话题」——谁在聊都算。
    情绪标签不参与建向量，标签匹配由 labels 那一路单独负责
    混在一起时实测命中率 28.6%，单独用 text 是 47.4%。
    """
    return {
        KIND_TEXT: (text_in_image or "").strip(),
        KIND_CAPTION: (caption or "").strip(),
        # 只装话题词。曾经把情绪标签也拼进来，结果没有话题的图（立绘、壁纸）
        # 仍然有一根「无语」「开心」的向量，同情绪的一批图在这一路上同分，纯噪音。
        # 情绪匹配本来就由 labels 那一路负责。
        KIND_SCENE: "；".join(part for part in (scenes or ()) if part),
    }


@dataclass(slots=True)
class Embedder:
    """把宿主 provider 包成「永远不抛异常」的样子：失败返回 None，由调用方降级。"""

    provider: Any
    model: str
    timeout: float = 5.0

    @property
    def dim(self) -> int:
        try:
            return int(self.provider.get_dim())
        except Exception:  # provider 实现各异，拿不到就用默认
            return FALLBACK_DIM

    async def embed(self, text: str) -> tuple[float, ...] | None:
        text = (text or "").strip()
        if not text:
            return None
        try:
            raw = await asyncio.wait_for(self.provider.get_embedding(text), self.timeout)
        except Exception:  # 超时/网络/协议错误一律降级
            return None
        if not raw:
            return None
        return normalize(raw)

    async def embed_many(
        self, texts: list[str], batch_size: int = 16
    ) -> list[tuple[float, ...] | None]:
        """批量算；整体失败时就退化成逐条，避免一条坏文本拖垮全部。"""
        if not texts:
            return []
        results: list[tuple[float, ...] | None] = []
        for start in range(0, len(texts), batch_size):
            chunk = texts[start : start + batch_size]
            try:
                raw = await asyncio.wait_for(
                    self.provider.get_embeddings(chunk), self.timeout * 4
                )
            except Exception:
                raw = None
            if raw is None or len(raw) != len(chunk):
                for text in chunk:
                    results.append(await self.embed(text))
                continue
            for item in raw:
                results.append(normalize(item) if item else None)
        return results

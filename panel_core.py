"""面板纯逻辑：批量参数收敛、条目序列化、缩略图、上传校验。

这一层不 import 宿主框架，也不碰请求对象——`panel.py` 只做 request/response 适配。
这样「批量上限」「非法 id 剔除」「上传扩展名」「缩略图降级」都能单测。
"""

from __future__ import annotations

import base64
import logging
import re
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Callable

from .domain import (
    GENERIC_PERSONA,
    MAX_CAPTION,
    MAX_TEXT_IN_IMAGE,
    SCOPE_ANY,
    SCOPE_ORIGIN,
    Memo,
    label_display,
)
from .publish.gate import session_key

logger = logging.getLogger(__name__)

MAX_BATCH_IDS = 50
MAX_TERM_COUNT = 12
MAX_TERM_LEN = 20
THUMB_MAX_SIDE = 200
THUMB_QUALITY = 78
THUMB_CACHE_ENTRIES = 2048
PAGE_SIZE_DEFAULT = 12
PAGE_SIZE_MAX = 48
ALLOWED_UPLOAD_EXTS: tuple[str, ...] = (
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
)
ALLOWED_STATES: tuple[str, ...] = ("inbox", "ready", "disabled")
ALLOWED_SCOPES: tuple[str, ...] = ("any", "origin")


@dataclass(frozen=True, slots=True)
class PageArgs:
    page: int = 1
    size: int = PAGE_SIZE_DEFAULT
    state: str | None = None
    label: str | None = None
    persona: str | None = None
    keyword: str = ""
    offset: int = 0


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def parse_page_args(get: Callable[..., Any] | None) -> PageArgs:
    """把 query 参数收敛成安全的分页条件；非法值一律退回默认。"""

    def read(key: str, default: str = "") -> str:
        if get is None:
            return default
        try:
            value = get(key, default)
        except TypeError:  # 某些实现要求位置参数
            value = get(key)
        return str(value if value is not None else default).strip()

    page = max(1, _as_int(read("page", "1"), 1))
    requested = _as_int(read("size", str(PAGE_SIZE_DEFAULT)), PAGE_SIZE_DEFAULT)
    size = max(1, min(PAGE_SIZE_MAX, requested))
    state = read("state").lower()
    if state not in ALLOWED_STATES:
        state = None
    label = read("label").lower() or None
    # `__none__` 是前端「只看通用」的哨兵值，原样保留，不能归一成空串
    persona = read("persona") or None
    keyword = read("q")[:64]
    return PageArgs(
        page=page,
        size=size,
        state=state,
        label=label,
        persona=persona,
        keyword=keyword,
        offset=(page - 1) * size,
    )


def clamp_text(value: Any, limit: int = MAX_CAPTION) -> str:
    """单行文本：压空白、去首尾、限长。"""
    return re.sub(r"\s+", " ", str(value or "")).strip()[:limit]


def split_terms(
    value: Any, limit: int = MAX_TERM_COUNT, item_limit: int = MAX_TERM_LEN
) -> list[str]:
    """把「猫，手绘；Q版」这类输入拆成去重后的短词；也接受已有的列表。"""
    if isinstance(value, (list, tuple)):
        raw = [str(item) for item in value]
    else:
        raw = re.split(r"[,，;；/、|]+", str(value or ""))
    result: list[str] = []
    for item in raw:
        text = clamp_text(item, item_limit)
        if text and text not in result:
            result.append(text)
        if len(result) >= limit:
            break
    return result


def import_params(
    source: dict[str, Any],
) -> tuple[str, str | None, str | None]:
    """解析 (scope, umo, persona_id)。

    作用域要「仅来源会话」却给不出合法会话时抛 ValueError——静默退化成全库
    会让本来只想在某个群用的图到处都能发。
    """
    scope = str(source.get("scope") or SCOPE_ANY).strip().lower()
    if scope not in (SCOPE_ANY, SCOPE_ORIGIN):
        scope = SCOPE_ANY
    raw_session = str(source.get("umo") or "").strip()
    umo: str | None = session_key(raw_session) if raw_session else None
    if scope == SCOPE_ORIGIN and not umo:
        raise ValueError("仅指定会话需要填 G:群号 或 F:QQ号")
    persona_id = str(source.get("persona_id") or "").strip() or None
    return scope, umo, persona_id


UPLOAD_JOB_RE = re.compile(r"[0-9a-f]{32}")

MANIFEST_MAX_ITEMS = 200
"""一次清单导入最多入队多少条：再多就该拆批，免得把队列和识图额度一次打满。"""


def manifest_urls(data: Any) -> list[str]:
    """从清单 JSON 里抽图片 URL。

    接受的形状：字符串数组，或 `{"items": [...]}`；每一项可以是 URL 字符串，
    也可以是 `{"url": "..."}`。其它字段一律忽略，坏项跳过而不是整份失败。
    """
    items = data.get("items") if isinstance(data, dict) else data
    if not isinstance(items, (list, tuple)):
        return []
    urls: list[str] = []
    for item in items:
        if isinstance(item, str):
            url = item.strip()
        elif isinstance(item, dict):
            url = str(item.get("url") or "").strip()
        else:
            continue
        if url and url not in urls:
            urls.append(url)
        if len(urls) >= MANIFEST_MAX_ITEMS:
            break
    return urls


def _persona_list(value: Any) -> list[str]:
    """人格 id 列表：接受数组或逗号串；`none`/`clear`/空 表示清空。"""
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value or "").strip()
    if text.lower() in ("none", "clear", "", "-"):
        return []
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


def persona_params(body: dict[str, Any]) -> tuple[list[str] | None, list[str] | None]:
    """解析 (可用名单, 禁用人格名单)；字段没传时返回 None，表示这一项不动。

    两个名单互相独立：可用名单里的 `*` 是「通用」，禁用人格按 id 记，判定时禁用优先。
    只传禁用就等于「原本通用的表情，只是这一个人格不发」，不需要枚举其他人格。
    """
    allow: list[str] | None = None
    if "persona_ids" in body:
        allow = _persona_list(body.get("persona_ids"))
    elif "persona_id" in body:
        allow = _persona_list(body.get("persona_id"))
    block: list[str] | None = None
    if "block_persona_ids" in body:
        block = _persona_list(body.get("block_persona_ids"))
    return allow, block


def is_upload_job(job: str) -> bool:
    """上传返回的 job 必须是 32 位小写 hex（uuid4().hex）。

    不校验的话，job 里塞 `/` 或 `..` 就能指到 imports/ 之外；
    虽然 resolve_within 会再拦一次，但这里先拒掉更清楚。
    """
    return bool(UPLOAD_JOB_RE.fullmatch(str(job or "").strip()))


def clamp_ids(raw: Any, limit: int = MAX_BATCH_IDS) -> list[int]:
    """批量操作入参收敛：去重保序、剔除非法与越界值、硬性截断。"""
    if raw is None:
        return []
    items = [raw] if isinstance(raw, (int, str)) else list(raw)
    result: list[int] = []
    for item in items:
        memo_id = _as_int(item, 0)
        if memo_id <= 0 or memo_id in result:
            continue
        result.append(memo_id)
        if len(result) >= max(1, limit):
            break
    return result


def normalize_upload_name(filename: str | None) -> tuple[str, str | None]:
    """返回 (安全字名, 扩展名)；扩展名不在白名单时返回 (stem, None)。"""
    raw = str(filename or "").replace("\\", "/").split("/")[-1].strip()
    if not raw or raw in (".", ".."):
        return "", None
    path = Path(raw)
    ext = path.suffix.lower()
    stem = path.stem[:40] or "upload"
    if ext not in ALLOWED_UPLOAD_EXTS:
        return stem, None
    return stem, ext


def serialize_memo(memo: Memo) -> dict[str, Any]:
    """网格卡片需要的字段；路径不外露（缩略图走 id）。"""
    return {
        "id": memo.id,
        "label": memo.label,
        "label_name": label_display(memo.label),
        "labels": [{"key": key, "name": label_display(key)} for key in memo.labels],
        "caption": memo.caption[:MAX_CAPTION],
        "text_in_image": memo.text_in_img[:MAX_TEXT_IN_IMAGE],
        "scenes": list(memo.scenes),
        "state": memo.state,
        "scope": memo.scope,
        "persona_ids": [pid for pid in memo.persona_ids if pid != GENERIC_PERSONA],
        "persona_generic": GENERIC_PERSONA in memo.persona_ids,
        "blocked_personas": list(memo.blocked_persona_ids),
        "has_origin": bool(memo.origin_umo),
        "is_fav": memo.is_fav,
        "use_count": memo.use_count,
        "created_at": memo.created_at,
    }


def keyword_hit(memo: Memo, keyword: str) -> bool:
    text = (keyword or "").strip().lower()
    if not text:
        return True
    return text in memo.haystack.lower()


GENERIC_FILTER = "__none__"
"""前端「（通用）」的哨兵值：只保留没有绑定人格的条目。"""


def filter_memos(memos: list[Memo], keyword: str, persona: str | None) -> list[Memo]:
    """关键词与人格在内存里过滤（SQL 侧已按状态/分类收敛）。"""
    result = []
    for memo in memos:
        if not keyword_hit(memo, keyword):
            continue
        if persona == GENERIC_FILTER:
            if not memo.persona_generic:
                continue
        elif persona and persona not in memo.persona_ids:
            continue
        result.append(memo)
    return result


def _flatten_to_rgb(image: object) -> object:
    """转成不透明的 RGB；只有真带透明通道的图才走白底合成。

    以前不分青红皂白先 convert("RGBA") 再建白底画布再 paste：
    一张 3840×2161 的 RGB 图光这两步就要 100ms，而它本来就不需要。
    """
    from PIL import Image

    mode = getattr(image, "mode", "RGB")
    if mode not in ("RGBA", "LA", "P", "PA"):
        return image.convert("RGB")  # type: ignore[attr-defined]
    frame = image.convert("RGBA")  # type: ignore[attr-defined]
    canvas = Image.new("RGB", frame.size, (255, 255, 255))
    canvas.paste(frame, mask=frame.split()[-1])
    return canvas


def thumb_bytes(source: Path, max_side: int = THUMB_MAX_SIDE) -> bytes | None:
    """生成 JPEG 缩略图字节；任何失败都返回 None，由前端显示占位。"""
    try:
        from PIL import Image

        if not source.is_file():
            return None
        with Image.open(source) as image:
            if int(getattr(image, "n_frames", 1)) > 1:
                image.seek(0)
            canvas = _flatten_to_rgb(image)
            canvas.thumbnail((max_side, max_side), Image.LANCZOS)
            buffer = BytesIO()
            canvas.save(buffer, format="JPEG", quality=THUMB_QUALITY, optimize=True)
            return buffer.getvalue()
    except Exception as exc:
        # 坏图/超限：面板显示「无缩略图」占位，这里留痕说明是哪类失败
        logger.debug("生成缩略图失败 type=%s", type(exc).__name__)
        return None


PREVIEW_MAX_SIDE = 960
PREVIEW_MAX_ANIMATED_BYTES = 8 * 1024 * 1024
PREVIEW_CACHE_ENTRIES = 64
# 超过这个像素量才值得调 draft()：小图 draft 反而多一次解码器交互
PREVIEW_SOFT_PIXELS = 4_000_000
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".apng": "image/apng",
}


def preview_bytes(source: Path, max_side: int = PREVIEW_MAX_SIDE) -> tuple[bytes, str] | None:
    """预览图字节：动图直接给原文件（保留动画），静态图给白底大尺寸 JPEG。

    超大图先用 `draft()` 让解码器直接降采样：一张 8000×8000 的 JPEG 全解要
    192MB 位图和几十秒，draft 之后只解出目标尺寸附近的图，快一个数量级。
    """
    try:
        from PIL import Image

        if not source.is_file():
            return None
        suffix = source.suffix.lower()
        size = source.stat().st_size
        with Image.open(source) as image:
            animated = int(getattr(image, "n_frames", 1)) > 1
            if animated and size <= PREVIEW_MAX_ANIMATED_BYTES:
                return source.read_bytes(), MIME_BY_SUFFIX.get(suffix, "image/gif")
            width, height = image.size
            if width * height > PREVIEW_SOFT_PIXELS:
                # 只对 JPEG 有效；PNG/WebP 的 draft 是空操作，靠下面的统一缩放兜底
                try:
                    image.draft("RGB", (max_side, max_side))
                except Exception as exc:
                    # draft 只是省解码开销的优化，失败不影响结果，但要能查到
                    logger.debug("draft 缩放失败 type=%s", type(exc).__name__)
            canvas = _flatten_to_rgb(image)
            canvas.thumbnail((max_side, max_side), Image.LANCZOS)
            buffer = BytesIO()
            canvas.save(buffer, format="JPEG", quality=88, optimize=True)
            return buffer.getvalue(), "image/jpeg"
    except Exception as exc:
        logger.debug("生成预览图失败 type=%s", type(exc).__name__)
        return None


def thumb_payload(data: bytes) -> dict[str, Any]:
    """字段名不能叫 data：宿主 bridge 会把顶层 data 当成信封拆掉。"""
    return {
        "ok": True,
        "mime": "image/jpeg",
        "image": base64.b64encode(data).decode("ascii"),
    }


class ThumbCache:
    """按 (rel_path, mtime) 缓存缩略图字节；满了淘汰最旧的一批。

    文件一挪 rel_path 就变，旧条目自然 miss，所以写操作后不需要主动清空。
    """

    def __init__(self, limit: int = THUMB_CACHE_ENTRIES) -> None:
        self._limit = max(1, limit)
        self._items: dict[tuple[str, int], bytes] = {}

    def get(self, rel_path: str, mtime_ns: int) -> bytes | None:
        return self._items.get((rel_path, mtime_ns))

    def put(self, rel_path: str, mtime_ns: int, data: bytes) -> None:
        if len(self._items) >= self._limit:
            # 淘汰最早的四分之一，不全清：全清会让正在翻的整页缩略图重新解码，
            # 一次卡几百毫秒到几秒。dict 保插入顺序，前 N 个就是最旧的。
            drop = max(1, self._limit // 4)
            for key in list(self._items)[:drop]:
                del self._items[key]
        self._items[(rel_path, mtime_ns)] = data

    def clear(self) -> None:
        self._items.clear()

    def size(self) -> int:
        return len(self._items)


class PreviewCache:
    """按 (rel_path, mtime) 缓存预览字节与 MIME；满了淘汰最旧的一批。

    预览比缩略图大一个量级，所以条数少得多。MIME 和数据存成一条，
    不会再出现「数据被淘汰了、MIME 还留着」那种两张表对不上的状态。
    """

    def __init__(self, limit: int = PREVIEW_CACHE_ENTRIES) -> None:
        self._limit = max(1, limit)
        self._items: dict[tuple[str, int], tuple[bytes, str]] = {}

    def get(self, rel_path: str, mtime_ns: int) -> tuple[bytes, str] | None:
        return self._items.get((rel_path, mtime_ns))

    def put(self, rel_path: str, mtime_ns: int, data: bytes, mime: str) -> None:
        if len(self._items) >= self._limit:
            drop = max(1, self._limit // 4)
            for key in list(self._items)[:drop]:
                del self._items[key]
        self._items[(rel_path, mtime_ns)] = (data, mime)

    def clear(self) -> None:
        self._items.clear()

    def size(self) -> int:
        return len(self._items)


__all__ = [
    "ALLOWED_STATES",
    "ALLOWED_SCOPES",
    "ALLOWED_UPLOAD_EXTS",
    "MAX_BATCH_IDS",
    "PAGE_SIZE_MAX",
    "GENERIC_FILTER",
    "PageArgs",
    "PreviewCache",
    "ThumbCache",
    "MAX_CAPTION",
    "MAX_TERM_COUNT",
    "clamp_ids",
    "clamp_text",
    "filter_memos",
    "keyword_hit",
    "PREVIEW_MAX_SIDE",
    "_flatten_to_rgb",
    "PREVIEW_SOFT_PIXELS",
    "normalize_upload_name",
    "parse_page_args",
    "persona_params",
    "serialize_memo",
    "split_terms",
    "thumb_bytes",
    "thumb_payload",
]

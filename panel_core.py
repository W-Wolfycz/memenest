"""面板纯逻辑：批量参数收敛、条目序列化、缩略图、上传校验。

这一层不 import 宿主框架，也不碰请求对象——`panel.py` 只做 request/response 适配。
这样「批量上限」「非法 id 剔除」「上传扩展名」「缩略图降级」都能单测。
"""

from __future__ import annotations

import base64
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

MAX_BATCH_IDS = 50
MAX_TERM_COUNT = 12
MAX_TERM_LEN = 20
THUMB_MAX_SIDE = 200
THUMB_QUALITY = 78
THUMB_CACHE_ENTRIES = 256
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
    persona = read("persona") or None
    """`__none__` 是前端「只看通用」的哨兵值，不能归一成空串。"""
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


def thumb_bytes(source: Path, max_side: int = THUMB_MAX_SIDE) -> bytes | None:
    """生成 JPEG 缩略图字节；任何失败都返回 None，由前端显示占位。"""
    try:
        from PIL import Image

        if not source.is_file():
            return None
        with Image.open(source) as image:
            if int(getattr(image, "n_frames", 1)) > 1:
                image.seek(0)
            frame = image.convert("RGBA")
            canvas = Image.new("RGB", frame.size, (255, 255, 255))
            canvas.paste(frame, mask=frame.split()[-1])
            canvas.thumbnail((max_side, max_side), Image.LANCZOS)
            buffer = BytesIO()
            canvas.save(buffer, format="JPEG", quality=THUMB_QUALITY, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None


PREVIEW_MAX_SIDE = 960
PREVIEW_MAX_ANIMATED_BYTES = 8 * 1024 * 1024
MIME_BY_SUFFIX = {
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".webp": "image/webp",
    ".bmp": "image/bmp",
    ".apng": "image/apng",
}


def preview_payload(source: Path, max_side: int = PREVIEW_MAX_SIDE) -> dict[str, Any] | None:
    """预览用载荷：动图直接给原文件（保留动画），静态图给白底大尺寸 JPEG。"""
    try:
        from PIL import Image

        if not source.is_file():
            return None
        suffix = source.suffix.lower()
        size = source.stat().st_size
        with Image.open(source) as image:
            animated = int(getattr(image, "n_frames", 1)) > 1
            if animated and size <= PREVIEW_MAX_ANIMATED_BYTES:
                return {
                    "mime": MIME_BY_SUFFIX.get(suffix, "image/gif"),
                    "image": base64.b64encode(source.read_bytes()).decode("ascii"),
                }
            frame = image.convert("RGBA")
            canvas = Image.new("RGB", frame.size, (255, 255, 255))
            canvas.paste(frame, mask=frame.split()[-1])
            canvas.thumbnail((max_side, max_side), Image.LANCZOS)
            buffer = BytesIO()
            canvas.save(buffer, format="JPEG", quality=88, optimize=True)
            return {
                "mime": "image/jpeg",
                "image": base64.b64encode(buffer.getvalue()).decode("ascii"),
            }
    except Exception:
        return None


def thumb_payload(data: bytes) -> dict[str, Any]:
    """字段名不能叫 data：宿主 bridge 会把顶层 data 当成信封拆掉。"""
    return {
        "ok": True,
        "mime": "image/jpeg",
        "image": base64.b64encode(data).decode("ascii"),
    }


class ThumbCache:
    """按 (rel_path, mtime) 缓存缩略图字节；写操作后由平台侧整体清空。"""

    def __init__(self, limit: int = THUMB_CACHE_ENTRIES) -> None:
        self._limit = max(1, limit)
        self._items: dict[tuple[str, int], bytes] = {}

    def get(self, rel_path: str, mtime_ns: int) -> bytes | None:
        return self._items.get((rel_path, mtime_ns))

    def put(self, rel_path: str, mtime_ns: int, data: bytes) -> None:
        if len(self._items) >= self._limit:
            self._items.clear()
        self._items[(rel_path, mtime_ns)] = data

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
    "ThumbCache",
    "MAX_CAPTION",
    "MAX_TERM_COUNT",
    "clamp_ids",
    "clamp_text",
    "filter_memos",
    "keyword_hit",
    "PREVIEW_MAX_SIDE",
    "normalize_upload_name",
    "preview_payload",
    "parse_page_args",
    "serialize_memo",
    "split_terms",
    "thumb_bytes",
    "thumb_payload",
]

"""把媒体引用抓成本地临时文件（F3.1 / F3.2 的前半段）。

三条铁律：
1. 返回路径一定落在 tmp/ 内，扩展名由真实内容决定，不用用户可控字符串拼路径。
2. 任何失败都返回原因码，不抛异常（R-6 需要区分瞬时/永久/系统级）。
3. URL 每一跳重定向都重新过 security.check_url，体积边下边算。
"""

from __future__ import annotations

import base64
import binascii
import uuid
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path

from ..domain import MediaRef
from . import security
from .fingerprints import content_hash, percept_hash

ALLOWED_EXTS: tuple[str, ...] = (".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".apng")
MAX_REDIRECTS = 3
DOWNLOAD_TIMEOUT_SECONDS = 30.0
HTTP_TRANSIENT_STATUS = (408, 425, 429)


@dataclass(frozen=True, slots=True)
class FetchLimits:
    max_bytes: int = 32 * 1024 * 1024
    max_pixels: int = 40_000_000
    allowed_exts: tuple[str, ...] = ALLOWED_EXTS


@dataclass(slots=True)
class FetchedMedia:
    path: Path
    ext: str
    content_hash: str
    percept_hash: str | None
    byte_size: int
    pixels: int
    is_animated: bool


@dataclass(slots=True)
class FetchResult:
    media: FetchedMedia | None
    reason: str

    @property
    def ok(self) -> bool:
        return self.media is not None


def _decode_data_uri(source: str) -> bytes | None:
    if not source.startswith("data:"):
        return None
    header, _, payload = source.partition(",")
    if ";base64" not in header:
        return None
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def _decode_base64(source: str) -> bytes | None:
    payload = source[len("base64://") :]
    try:
        return base64.b64decode(payload, validate=False)
    except (binascii.Error, ValueError):
        return None


def _read_local(source: str) -> tuple[bytes | None, str]:
    raw = source[len("file://") :] if source.startswith("file://") else source
    # file:///C:/x 与 file://C:/x 两种写法都要认。
    if raw.startswith("/") and len(raw) > 3 and raw[2] == ":":
        raw = raw[1:]
    try:
        path = Path(raw)
    except (OSError, ValueError):
        return None, "local_path_invalid"
    try:
        if not path.is_file():
            return None, "local_missing"
        return path.read_bytes(), "ok"
    except OSError:
        return None, "local_read_failed"


async def _download(url: str, limits: FetchLimits) -> tuple[bytes | None, str]:
    try:
        import httpx
    except ImportError:  # pragma: no cover - requirements 已声明
        return None, "http_client_missing"

    current = url
    timeout = httpx.Timeout(DOWNLOAD_TIMEOUT_SECONDS, connect=10.0)
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            for _ in range(MAX_REDIRECTS + 1):
                allowed, reason = security.check_url(current)
                if not allowed:
                    return None, reason
                async with client.stream("GET", current) as response:
                    status = response.status_code
                    if status in (301, 302, 303, 307, 308):
                        current = str(response.headers.get("location") or "").strip()
                        if not current:
                            return None, "redirect_without_target"
                        continue
                    if status in HTTP_TRANSIENT_STATUS or status >= 500:
                        return None, f"http_{status}"
                    if status != 200:
                        return None, f"http_{status}"
                    declared = response.headers.get("content-length")
                    if declared and declared.isdigit() and int(declared) > limits.max_bytes:
                        return None, "oversize"
                    buffer = bytearray()
                    async for chunk in response.aiter_bytes(64 * 1024):
                        buffer.extend(chunk)
                        if len(buffer) > limits.max_bytes:
                            return None, "oversize"
                    return bytes(buffer), "ok"
            return None, "too_many_redirects"
    except Exception as exc:  # 网络层异常一律按瞬时处理
        return None, f"network_{type(exc).__name__}"


async def _read_source(ref: MediaRef, limits: FetchLimits) -> tuple[bytes | None, str]:
    source = (ref.source or "").strip()
    if not source:
        return None, "empty_source"
    if source.startswith("base64://"):
        data = _decode_base64(source)
        return (data, "ok") if data else (None, "base64_invalid")
    if source.startswith("data:"):
        data = _decode_data_uri(source)
        return (data, "ok") if data else (None, "data_uri_invalid")
    if source.startswith(("http://", "https://")):
        return await _download(source, limits)
    if source.startswith("file://") or "://" not in source:
        data, reason = _read_local(source)
        return data, reason
    return None, "scheme_unsupported"


def _inspect(data: bytes, limits: FetchLimits) -> tuple[str, int, bool, str]:
    """用真实内容确认格式/像素/是否动图；返回 (ext, pixels, animated, reason)。"""
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        return "", 0, False, "pillow_missing"
    try:
        with Image.open(BytesIO(data)) as image:
            image_format = (image.format or "").lower()
            width, height = image.size
            animated = int(getattr(image, "n_frames", 1)) > 1
    except Exception:
        return "", 0, False, "decode_failed"
    ext_map = {
        "png": ".png",
        "jpeg": ".jpg",
        "gif": ".gif",
        "webp": ".webp",
        "bmp": ".bmp",
        "apng": ".apng",
    }
    ext = ext_map.get(image_format, "")
    if not ext or ext not in limits.allowed_exts:
        return "", 0, False, "format_denied"
    pixels = int(width) * int(height)
    if pixels <= 0:
        return "", 0, False, "decode_failed"
    if pixels > limits.max_pixels:
        return "", pixels, animated, "too_many_pixels"
    return ext, pixels, animated, "ok"


async def fetch_media(ref: MediaRef, limits: FetchLimits, tmp_dir: Path) -> FetchResult:
    data, reason = await _read_source(ref, limits)
    if data is None:
        return FetchResult(None, reason)
    if not data:
        return FetchResult(None, "empty_body")
    if len(data) > limits.max_bytes:
        return FetchResult(None, "oversize")
    ext, pixels, animated, inspect_reason = _inspect(data, limits)
    if not ext:
        return FetchResult(None, inspect_reason)

    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        path = tmp_dir / f"{uuid.uuid4().hex}.bin"
        path.write_bytes(data)
    except OSError:
        return FetchResult(None, "disk_write_failed")

    percept = None
    try:
        from PIL import Image

        with Image.open(BytesIO(data)) as image:
            percept = percept_hash(image)
    except Exception:
        percept = None

    media = FetchedMedia(
        path=path,
        ext=ext,
        content_hash=content_hash(data),
        percept_hash=percept,
        byte_size=len(data),
        pixels=pixels,
        is_animated=animated,
    )
    return FetchResult(media, "ok")


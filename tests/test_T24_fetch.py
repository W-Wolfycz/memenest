"""T-24 / R-6：抓取层的行为边界。

守的静默失效：重定向逐跳校验被去掉（SSRF）、体积上限只查声明不查实际、
扩展名按 URL 走而不是按真实内容、临时文件落到 tmp/ 之外。
"""

from __future__ import annotations

from io import BytesIO

import pytest
from PIL import Image

from memenest.domain import MediaRef
from memenest.ingest import fetch


def _png_bytes(size: int = 16) -> bytes:
    buffer = BytesIO()
    Image.new("RGB", (size, size), (10, 20, 30)).save(buffer, format="PNG")
    return buffer.getvalue()


def _ref(source: str, ext: str = ".png") -> MediaRef:
    return MediaRef(
        source=source,
        declared_ext=ext,
        origin_kind="chat",
        platform="aiocqhttp",
        reason="sticker_subtype",
    )


class _FakeResponse:
    def __init__(self, status: int, headers: dict | None = None, body: bytes = b"") -> None:
        self.status_code = status
        self.headers = headers or {}
        self._body = body

    async def aiter_bytes(self, chunk: int = 65536):
        yield self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeClient:
    def __init__(self, routes: dict[str, _FakeResponse]) -> None:
        self.routes = routes
        self.requested: list[str] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def stream(self, method: str, url: str):
        self.requested.append(url)
        return self.routes.get(url, _FakeResponse(404))


def _install_fake_httpx(monkeypatch, routes: dict[str, _FakeResponse]):
    import httpx

    client = _FakeClient(routes)
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: client)
    return client


@pytest.mark.asyncio
async def test_redirect_into_private_host_is_refused(tmp_path, monkeypatch):
    """第二跳的重定向必须重新过 security.check_url，否则就是 SSRF。"""
    routes = {
        "https://cdn.example.com/a.png": _FakeResponse(
            302, {"location": "http://127.0.0.1/secret"}
        ),
    }
    _install_fake_httpx(monkeypatch, routes)
    result = await fetch.fetch_media(
        _ref("https://cdn.example.com/a.png"), fetch.FetchLimits(), tmp_path
    )
    assert result.ok is False
    assert result.reason == "url_host_denied"
    # 被拒绝时不允许把内容落到 tmp/
    assert list(tmp_path.iterdir()) == []


@pytest.mark.asyncio
async def test_extension_follows_real_content_not_url(tmp_path, monkeypatch):
    """URL 写着 .gif、内容其实是 PNG：落盘扩展名必须是 .png，且在 tmp/ 内。"""
    routes = {
        "https://cdn.example.com/fake.gif": _FakeResponse(200, {}, _png_bytes()),
    }
    _install_fake_httpx(monkeypatch, routes)
    result = await fetch.fetch_media(
        _ref("https://cdn.example.com/fake.gif", ".gif"), fetch.FetchLimits(), tmp_path
    )
    assert result.ok is True
    assert result.media is not None
    assert result.media.ext == ".png"
    assert result.media.path.parent == tmp_path
    assert result.media.path.is_file()


@pytest.mark.asyncio
async def test_animated_media_gets_composite_percept_hash(tmp_path):
    """动图用三帧复合指纹，不再只拿首帧。

    只比首帧会把「首帧相同、后续不同」的动画判成同一张，新表情被静默丢掉；
    复合指纹要求三帧都在阈值内才判重。这里验证动图路径产出带分隔的复合值。
    """

    def stripes(offset: int) -> Image.Image:
        """黑白条纹：GIF 量化后亮暗差仍在，能保证首帧算得出非空指纹。"""
        image = Image.new("L", (32, 32))
        pixels = image.load()
        for x in range(32):
            for y in range(32):
                pixels[x, y] = 255 if ((x + offset) // 4) % 2 else 0
        return image.convert("RGB")

    frames = [stripes(0), stripes(4)]
    source = tmp_path / "anim.gif"
    frames[0].save(source, save_all=True, append_images=frames[1:], duration=100, loop=0)
    result = await fetch.fetch_media(
        _ref(str(source), ".gif"), fetch.FetchLimits(), tmp_path / "tmp"
    )
    assert result.ok is True
    assert result.media is not None
    assert result.media.is_animated is True
    parts = result.media.percept_hash.split(":")
    assert len(parts) == 3, "复合指纹必须是三段"
    assert all(part and int(part, 16) >= 0 for part in parts)

"""T-21 / T-22 / T-24 / T-27 / R-8：黑名单、容量淘汰、抓取防护与分页。

守的静默失效：拉黑过的指纹又回来（用户明确拒绝的内容反复出现）、
淘汰把收藏删掉、翻页排序键没有索引导致全表扫描、工具入口能访问内网地址。
"""

from __future__ import annotations

import pytest

from memenest.domain import STATE_READY, Analysis, Draft, PageQuery
from memenest.ingest.security import check_url
from memenest.store import MemoStore


def _draft(rel_path: str, *, state: str = STATE_READY, origin_kind: str = "chat") -> Draft:
    return Draft(
        rel_path=rel_path,
        content_hash=rel_path.encode().hex().ljust(64, "a")[:64],
        percept_hash=None,
        analysis=Analysis(label="joy", caption=rel_path),
        scope="any",
        origin_umo="aiocqhttp:GroupMessage:1",
        origin_kind=origin_kind,
        created_at=1,
        state=state,
    )


@pytest.fixture()
def store(tmp_path):
    instance = MemoStore(tmp_path / "memo.db")
    instance.initialize()
    yield instance
    instance.close()


def test_T22_eviction_keeps_favourites_and_imports(store):
    """淘汰必须先跳过收藏与导入：顺序算错会把用户手动留下的图一起删掉。"""
    first = store.add(_draft("inbox/a.png"))
    second = store.add(_draft("inbox/b.png"))
    favourite = store.add(_draft("inbox/c.png"))
    imported = store.add(_draft("inbox/d.png", origin_kind="import"))
    assert None not in (first, second, favourite, imported)
    store.set_fav(favourite, True)

    removed = store.evict(cap=2)
    assert len(removed) == 2
    remaining = {memo.id for memo in store.page(PageQuery())[0]}
    assert favourite in remaining
    assert imported in remaining
    assert set(removed) == {"inbox/a.png", "inbox/b.png"}


def test_T24_url_checks():
    """URL 只接受 http/https 且主机必须是公网：协议白名单与私网拦截缺一不可。"""
    assert check_url("http://127.0.0.1/x") == (False, "url_host_denied")
    assert check_url("http://169.254.169.254/latest/meta-data/") == (False, "url_host_denied")
    # 内网域名后缀同样要拦，别只拦 IP 字面量
    assert check_url("http://x.internal/") == (False, "url_host_denied")
    assert check_url("file:///etc/passwd") == (False, "url_scheme_denied")
    assert check_url("gopher://example.com/") == (False, "url_scheme_denied")
    assert check_url("https://cdn.example.com/a.png") == (True, "url_ok")
    assert check_url("") == (False, "url_empty")


@pytest.mark.parametrize(
    "host", ["2130706433", "127.1"]
)
def test_T24_numeric_host_forms_are_forbidden(host):
    """127.1 / 整数 / 十六进制写法：部分平台的 getaddrinfo 会解析成内网地址。"""
    assert check_url(f"http://{host}/x") == (False, "url_host_denied")

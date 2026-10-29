"""面板纯逻辑：批量参数、分页收敛、上传校验、缩略图、缓存。

守的静默失效：批量接口接受任意 id 列表（一次删掉半库）、分页参数越界导致
全表扫描、上传文件名带路径穿越、缩略图生成失败让整页空白。
"""

from __future__ import annotations

import pytest

from memenest import panel_core as core


def _query(mapping):
    def get(key, default=None, type=None):  # noqa: A002 - 与宿主签名保持一致
        return mapping.get(key, default)

    return get


def test_normalize_upload_name_strips_paths_and_checks_extensions():
    """上传文件名要剥掉路径并卡扩展名白名单：`../` 能直接把文件写到数据目录之外。"""
    assert core.normalize_upload_name("../../evil.png") == ("evil", ".png")
    assert core.normalize_upload_name(r"C:\tmp\photo.JPG") == ("photo", ".jpg")
    assert core.normalize_upload_name("anim.GIF") == ("anim", ".gif")
    assert core.normalize_upload_name("script.exe") == ("script", None)
    assert core.normalize_upload_name(None) == ("", None)
    assert core.normalize_upload_name("..") == ("", None)


def test_origin_without_session_is_rejected():
    """只想要某个群却说不出是哪个群：必须拒绝，不能退化成全库可发。"""
    with pytest.raises(ValueError):
        core.import_params({"scope": "origin", "umo": ""})
    with pytest.raises(ValueError):
        core.import_params({"scope": "origin", "umo": "乱填的"})


def test_is_upload_job_rejects_non_hex():
    """job 会被直接拼进数据目录路径：只认 uuid4().hex，
    放进 `/` 或 `..` 就能指到 imports/ 之外。"""
    assert core.is_upload_job("0123456789abcdef0123456789abcdef")
    assert core.is_upload_job("a" * 32)

    for bad in ["../evil", "..", "", "a" * 31, "A" * 32, "a/b", "a" * 32 + "b", None]:
        assert not core.is_upload_job(bad), bad

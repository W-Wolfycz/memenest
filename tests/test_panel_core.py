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


def test_persona_params_keeps_allow_and_block_apart():
    """两个名单互相独立：只传禁用时不能把可用名单悄悄清空。

    写坏的表现是「禁用一个人格，结果整条变成谁都不用」——界面上只显示禁用徽章，
    可用名单的变化要专门去看才发现。
    """
    allow, block = core.persona_params({"block_persona_ids": ["p1"]})
    assert allow is None, "没传可用名单 = 不动它"
    assert block == ["p1"]

    allow, block = core.persona_params({"persona_ids": ["*"], "block_persona_ids": ""})
    assert allow == ["*"]
    assert block == []

    # 逗号串与数组等价；none/clear 表示清空，不是「一个叫 none 的人格」
    assert core.persona_params({"persona_ids": "p1，p2"}) == (["p1", "p2"], None)
    assert core.persona_params({"persona_ids": "none"}) == ([], None)


def test_manifest_urls_skips_bad_items_without_failing_whole_list():
    """清单里个别坏项要跳过，不能整份失败。

    整份失败的表现是「导入清单后一条都没进来」，用户只会以为导入坏了；
    正确的行为是能用的都入队、坏的忽略。
    """
    assert core.manifest_urls(["https://a/1.png", {"url": "https://a/2.png"}, 3, ""]) == [
        "https://a/1.png",
        "https://a/2.png",
    ]
    assert core.manifest_urls({"items": [{"url": "https://a/3.png"}]}) == [
        "https://a/3.png"
    ]
    # 去重 + 上限
    assert core.manifest_urls(["https://a/1.png", "https://a/1.png"]) == [
        "https://a/1.png"
    ]
    many = [f"https://a/{i}.png" for i in range(core.MANIFEST_MAX_ITEMS + 50)]
    assert len(core.manifest_urls(many)) == core.MANIFEST_MAX_ITEMS
    assert core.manifest_urls({"nope": 1}) == []

"""选中导出：把挑中的图片打成 zip 交给浏览器下载。

守的静默失效：文件已经不在磁盘上的条目被悄悄跳过却仍告诉用户「导出成功」；
以及文件名里出现冒号（Windows 下无效，用户拿到的是下载失败）。
"""

from __future__ import annotations

import re
import zipfile
from datetime import datetime
from io import BytesIO

from memenest.backup import build_selection_zip, selection_name


def test_selection_name_is_filesystem_safe_with_millis():
    name = selection_name(datetime(2001, 2, 3, 4, 5, 6, 7000))
    assert name == "2001-02-03T04_05_06_007.zip"
    # 冒号在 Windows 上是非法字符，不能被写进文件名
    assert ":" not in name
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}_\d{2}_\d{2}_\d{3}\.zip", name)


def test_build_selection_zip_counts_skipped_files(tmp_path):
    (tmp_path / "blobs").mkdir()
    (tmp_path / "blobs" / "a.png").write_bytes(b"aaa")
    (tmp_path / "blobs" / "b.png").write_bytes(b"bbbb")

    result = build_selection_zip(
        tmp_path,
        ["blobs/a.png", "blobs/gone.png", "blobs/b.png", "../escape.png"],
    )

    assert result["included"] == 2
    assert result["skipped"] == 2, "缺失文件与越界路径都要计入跳过"
    with zipfile.ZipFile(BytesIO(result["data"])) as bundle:
        assert sorted(bundle.namelist()) == ["a.png", "b.png"]

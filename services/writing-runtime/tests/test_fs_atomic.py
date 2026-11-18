"""fs_atomic.replace_with_retry 的行为钉子。

背景：Windows 上杀软/索引器短暂握住目标文件时 os.replace 间歇性报
WinError 5，2026-09-12 全量 pytest 三次运行里批量写章记录落盘每次都因此
有用例判死（每次倒的还不是同一个）。重试语义由这里钉住。
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from zhimeng_runtime.fs_atomic import replace_with_retry


def test_replaces_normally(tmp_path: Path) -> None:
    source = tmp_path / "a.tmp"
    target = tmp_path / "b.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")

    replace_with_retry(source, target)

    assert target.read_text(encoding="utf-8") == "new"
    assert not source.exists()


def test_retries_permission_error_then_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.tmp"
    target = tmp_path / "b.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")

    real_replace = os.replace
    calls = {"count": 0}

    def flaky(src: object, dst: object) -> None:
        calls["count"] += 1
        if calls["count"] == 1:
            raise PermissionError(5, "拒绝访问。")
        real_replace(src, dst)  # type: ignore[arg-type]

    monkeypatch.setattr("zhimeng_runtime.fs_atomic.time.sleep", lambda _s: None)
    monkeypatch.setattr("zhimeng_runtime.fs_atomic.os.replace", flaky)

    replace_with_retry(source, target)

    assert calls["count"] == 2
    assert target.read_text(encoding="utf-8") == "new"


def test_gives_up_after_attempts_and_reraises(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "a.tmp"
    target = tmp_path / "b.json"
    source.write_text("new", encoding="utf-8")
    target.write_text("old", encoding="utf-8")

    def always_denied(src: object, dst: object) -> None:
        raise PermissionError(5, "拒绝访问。")

    monkeypatch.setattr("zhimeng_runtime.fs_atomic.time.sleep", lambda _s: None)
    monkeypatch.setattr("zhimeng_runtime.fs_atomic.os.replace", always_denied)

    with pytest.raises(PermissionError):
        replace_with_retry(source, target, attempts=3)

    assert target.read_text(encoding="utf-8") == "old"


def test_other_oserror_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "missing.tmp"
    target = tmp_path / "b.json"
    calls = {"count": 0}

    def not_found(src: object, dst: object) -> None:
        calls["count"] += 1
        raise FileNotFoundError(2, "no such file")

    monkeypatch.setattr("zhimeng_runtime.fs_atomic.os.replace", not_found)

    with pytest.raises(FileNotFoundError):
        replace_with_retry(source, target)

    assert calls["count"] == 1

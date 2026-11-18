from types import SimpleNamespace

import pytest

from zhimeng_runtime import environment_context


@pytest.mark.parametrize(
    ("version", "product_type", "expected"),
    [
        ((10, 0, 22631), 1, "Windows 11（10.0.22631）"),
        ((10, 0, 19045), 1, "Windows 10（10.0.19045）"),
        ((10, 0, 26100), 3, "Windows Server（10.0.26100）"),
    ],
)
def test_windows_environment_does_not_query_wmi(monkeypatch, version, product_type, expected):
    def unavailable():
        raise AssertionError("platform information may wait indefinitely for WMI")

    monkeypatch.setattr(environment_context.platform, "system", unavailable)
    monkeypatch.setattr(environment_context.platform, "version", unavailable)
    monkeypatch.setattr(environment_context.platform, "release", unavailable)
    monkeypatch.setattr(environment_context, "sys", SimpleNamespace(
        platform="win32",
        getwindowsversion=lambda: SimpleNamespace(platform_version=version, product_type=product_type),
    ))
    assert environment_context.system_description() == expected


def test_windows_version_error_does_not_prevent_starting_a_task(monkeypatch):
    def unavailable():
        raise OSError("version unavailable")

    monkeypatch.setattr(environment_context, "sys", SimpleNamespace(
        platform="win32", getwindowsversion=unavailable,
    ))
    assert environment_context.system_description() == "Windows"

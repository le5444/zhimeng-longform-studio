"""Windows 文件锁下的原子替换。

本机（杀软/索引器）会短暂握住刚写出的目标文件，``os.replace`` 间歇性报
WinError 5（拒绝访问）。2026-09-12 全量 pytest 三次运行里，批量写章的
记录落盘每次都有用例因此判死（每次倒的还不是同一个）。临时文件 + replace
的写法本身是对的，缺的只是对这种瞬时锁的短退避重试。
"""

from __future__ import annotations

import os
import time
from pathlib import Path


def replace_with_retry(source: Path, target: Path, *, attempts: int = 6) -> None:
    """``os.replace``，遇 PermissionError（WinError 5/32）按短退避重试。

    其余 OSError 照抛——真正没权限、路径坏了这类问题不该被重试掩盖。
    最坏总等待约 1.5 秒，正常一次就过。
    """
    delay = 0.05
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(delay)
            delay *= 2

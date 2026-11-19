"""Version-aware commits for journal files, including Windows sharing retries.

On Windows a delete-capable handle excludes outside writes and renames while we
verify the file and move it out of the destination. The replacement is installed
without replacing any file created in that interval. Journal backups are durable
before these operations begin; an interrupted update can therefore be recovered.
"""
from __future__ import annotations

from contextlib import contextmanager, suppress
import hashlib
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import BinaryIO, Callable, Iterator
from uuid import uuid4


class FileCommitError(OSError):
    def __init__(self, path: Path, *, conflict: bool = True, untouched: bool = True) -> None:
        message = (
            f"「{path}」读取后已被修改、移动或创建，现有内容已保留；重新读取当前文件后可以继续。"
            if conflict else
            f"「{path}」暂时无法提交，现有文件和可用备份已保留；检查文件占用或权限后可以重试。"
        )
        super().__init__(message)
        self.conflict = conflict
        self.untouched = untouched


def _digest(stream: BinaryIO) -> str:
    stream.seek(0)
    digest = hashlib.sha256()
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        digest.update(chunk)
    stream.seek(0)
    return digest.hexdigest()


def _win_path(path: Path) -> str:
    text = str(path.absolute())
    if text.startswith("\\\\?\\"):
        return text
    return "\\\\?\\UNC\\" + text[2:] if text.startswith("\\\\") else "\\\\?\\" + text


class _WindowsFile:
    def __init__(self, handle: int, stream: BinaryIO, kernel: object) -> None:
        self.handle, self.stream, self.kernel = handle, stream, kernel
        self.changed = False

    def rename(self, target: Path) -> None:
        import ctypes
        from ctypes import wintypes

        encoded = _win_path(target).encode("utf-16-le")

        class RenameInfo(ctypes.Structure):
            _fields_ = [
                ("ReplaceIfExists", wintypes.DWORD), ("RootDirectory", wintypes.HANDLE),
                ("FileNameLength", wintypes.DWORD), ("FileName", ctypes.c_ubyte * (len(encoded) + 2)),
            ]

        info = RenameInfo(0, None, len(encoded))
        ctypes.memmove(ctypes.addressof(info) + RenameInfo.FileName.offset, encoded, len(encoded))
        if not self.kernel.SetFileInformationByHandle(self.handle, 3, ctypes.byref(info), ctypes.sizeof(info)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.changed = True

    def delete(self) -> None:
        import ctypes
        from ctypes import wintypes

        flag = wintypes.BOOL(True)
        if not self.kernel.SetFileInformationByHandle(self.handle, 4, ctypes.byref(flag), ctypes.sizeof(flag)):
            raise ctypes.WinError(ctypes.get_last_error())
        self.changed = True


@contextmanager
def _windows_file(path: Path, expected: str) -> Iterator[_WindowsFile]:
    import ctypes
    from ctypes import wintypes
    import msvcrt

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
                                  wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    kernel.CreateFileW.restype = wintypes.HANDLE
    kernel.SetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.SetFileInformationByHandle.restype = wintypes.BOOL
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel.CloseHandle.restype = wintypes.BOOL
    # GENERIC_READ | DELETE; FILE_SHARE_READ excludes writes and path replacement.
    handle = kernel.CreateFileW(_win_path(path), 0x80010000, 1, None, 3, 0x00200000, None)
    if handle == ctypes.c_void_p(-1).value:
        error = ctypes.WinError(ctypes.get_last_error())
        if isinstance(error, FileNotFoundError):
            raise FileCommitError(path) from error
        raise error
    try:
        fd = msvcrt.open_osfhandle(handle, os.O_RDONLY | os.O_BINARY)
    except BaseException:
        kernel.CloseHandle(handle)
        raise
    lease = None
    try:
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(fd).st_mode) or _digest(stream) != expected:
                raise FileCommitError(path)
            lease = _WindowsFile(handle, stream, kernel)
            yield lease
    except OSError as exc:
        if not isinstance(exc, FileCommitError):
            exc.destination_untouched = lease is None or not lease.changed
        raise


def _install(source: Path, target: Path) -> None:
    """Install a fully written file without overwriting a newly occupied name."""
    if os.name == "nt":
        os.rename(source, target)
    else:
        os.link(source, target)
        with suppress(OSError):
            source.unlink()


def _retry(path: Path, operation: Callable[[], None]) -> None:
    delay = 0.05
    for attempt in range(6):
        try:
            operation()
            return
        except PermissionError as exc:
            untouched = getattr(exc, "destination_untouched", True)
            if attempt == 5 or not untouched:
                raise FileCommitError(path, conflict=False, untouched=untouched) from exc
            time.sleep(delay)
            delay *= 2
        except FileCommitError:
            raise
        except OSError as exc:
            raise FileCommitError(path, conflict=False,
                                  untouched=getattr(exc, "destination_untouched", True)) from exc


def _windows_update(source: Path, target: Path, expected: str) -> None:
    parked = target.with_name(f".{target.name}.{uuid4().hex}.replaced")
    keep_parked = False
    installed = False
    try:
        with _windows_file(target, expected) as original:
            original.rename(parked)
            try:
                _install(source, target)
                installed = True
            except BaseException as exc:
                try:
                    original.rename(target)
                except OSError as restore_error:
                    if not os.path.lexists(target):
                        # The journal's original backup remains authoritative.
                        keep_parked = True
                        raise FileCommitError(target, conflict=False, untouched=False) from restore_error
                if isinstance(exc, FileExistsError):
                    raise FileCommitError(target) from exc
                raise
    except OSError as exc:
        if not isinstance(exc, FileCommitError):
            exc.destination_untouched = not installed
        raise
    finally:
        # These are our own temporary bytes; current destination contents are
        # never removed here. A cleanup failure does not undo a confirmed save.
        if not keep_parked:
            with suppress(OSError):
                parked.unlink()


def commit_write(target: Path, content: bytes, expected: str | None) -> None:
    """None expects an absent destination; hashes refer to the prepared version."""
    try:
        fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
    except OSError as exc:
        raise FileCommitError(target, conflict=False) from exc
    source = Path(name)
    try:
        try:
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
        except OSError as exc:
            raise FileCommitError(target, conflict=False) from exc

        def commit() -> None:
            if expected is None:
                try:
                    _install(source, target)
                except FileExistsError as exc:
                    raise FileCommitError(target) from exc
            elif os.name == "nt":
                _windows_update(source, target, expected)
            else:
                # POSIX external programs need not honour advisory locks. Keep
                # per-attempt validation; Windows provides the stronger guard.
                try:
                    with target.open("rb") as current:
                        if _digest(current) != expected:
                            raise FileCommitError(target)
                except FileNotFoundError as exc:
                    raise FileCommitError(target) from exc
                os.replace(source, target)

        _retry(target, commit)
    finally:
        with suppress(OSError):
            source.unlink()


def commit_delete(path: Path, expected: str) -> None:
    def commit() -> None:
        if os.name == "nt":
            with _windows_file(path, expected) as current:
                current.delete()
        else:
            try:
                with path.open("rb") as current:
                    if _digest(current) != expected:
                        raise FileCommitError(path)
            except FileNotFoundError as exc:
                raise FileCommitError(path) from exc
            path.unlink()
    _retry(path, commit)


def commit_move(source: Path, target: Path, expected: str) -> None:
    def commit() -> None:
        if os.name != "nt":
            with source.open("rb") as current:
                if _digest(current) != expected:
                    raise FileCommitError(source)
            if os.path.lexists(target):
                raise FileCommitError(target)
            # Retain the existing cross-volume copy/compensation path.
            import shutil
            shutil.move(str(source), str(target))
            return
        with _windows_file(source, expected) as current:
            try:
                current.rename(target)
                return
            except FileExistsError as exc:
                raise FileCommitError(target) from exc
            except OSError as exc:
                if getattr(exc, "winerror", None) != 17:  # ERROR_NOT_SAME_DEVICE
                    raise
            fd, name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
            temporary = Path(name)
            try:
                with os.fdopen(fd, "wb") as output:
                    import shutil
                    shutil.copyfileobj(current.stream, output, 1024 * 1024)
                    output.flush()
                    os.fsync(output.fileno())
                try:
                    _install(temporary, target)
                except FileExistsError as exc:
                    raise FileCommitError(target) from exc
                try:
                    current.delete()
                except OSError as exc:
                    raise FileCommitError(source, conflict=False, untouched=False) from exc
            finally:
                with suppress(OSError):
                    temporary.unlink()
    _retry(source, commit)

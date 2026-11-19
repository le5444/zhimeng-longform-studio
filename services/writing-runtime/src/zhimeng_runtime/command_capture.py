"""Synchronous commands drain both pipes into masked logs with small previews."""
from __future__ import annotations

import os
from pathlib import Path
import threading
import time
from typing import BinaryIO, Callable
import uuid

from zhimeng_runtime.command_sessions import _OutputDecoder, _StreamMask, start_command_process, cleanup_command_script


def output_preview(head: str, tail: str, *, characters: int, limit: int,
                   hidden_lines: int, total_lines: int, saved: str | None) -> str:
    omitted = characters - limit
    if saved is None:
        marker = f"…（中间省略 {omitted} 字）…"
    else:
        search = (
            f"powershell -NoProfile -Command \"Select-String -Path '{saved}' -Pattern '要找的字'\""
            if os.name == "nt" else f"grep -n '要找的字' '{saved}'"
        )
        marker = (
            f"…（中间省略 {omitted} 字、约 {max(hidden_lines, 1)} 行；全文 {total_lines} 行存在 {saved}。"
            f"要在里面找某一行，搜这个文件：{search}；要整段看，用 read_file 读这个文件）…"
        )
    return f"{head}\n{marker}\n{tail}"


class _Capture:
    def __init__(self, folder: Path, name: str, limit: int, before_spill: Callable[[], None]):
        self.folder, self.name, self.limit = folder, name, limit
        self.before_spill = before_spill
        self.short = ""
        self.raw_short: str | None = ""
        self.head = self.tail = ""
        self.characters = self.newlines = 0
        self.pending_cr = ""
        self.stream: BinaryIO | None = None
        self.temporary: Path | None = None
        self.target: Path | None = None
        self.error: str | None = None
        self.masked = 0
        self.result: tuple[str, bool, str | None] | None = None

    def _failed(self, action: str, exc: Exception) -> None:
        self.error = f"{action} {self.name} 输出失败：{type(exc).__name__}；完整日志不可用。"
        if self.stream is not None:
            try:
                self.stream.close()
            except OSError:
                pass
            self.stream = None
        if self.temporary is not None:
            try:
                self.temporary.unlink(missing_ok=True)
            except OSError:
                pass

    def feed(self, text: str, *, final: bool = False) -> None:
        if self.raw_short is not None:
            self.raw_short = self.raw_short + text if len(self.raw_short) + len(text) <= self.limit else None
        text = self.pending_cr + text
        self.pending_cr = ""
        if not final and text.endswith("\r"):
            text, self.pending_cr = text[:-1], "\r"
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        if not text:
            return
        self.characters += len(text)
        self.newlines += text.count("\n")
        half = self.limit // 2
        self.head = (self.head + text)[:half]
        # One additional character makes the omitted-line count exact even
        # when the tail starts immediately after a newline.
        self.tail = (self.tail + text)[-half - 1:]
        if self.characters <= self.limit:
            self.short += text
            return
        if self.error is None:
            try:
                if self.stream is None:
                    self.before_spill()
                    self.folder.mkdir(parents=True, exist_ok=True)
                    name = f"cmd-{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:8]}-{self.name}.txt"
                    self.target = self.folder / name
                    self.temporary = self.folder / ("." + name + ".tmp")
                    self.stream = self.temporary.open("xb")
                    self.stream.write(self.short.encode("utf-8"))
                self.stream.write(text.encode("utf-8"))
            except OSError as exc:
                self._failed("保存", exc)
        self.short = ""

    def finish(self) -> tuple[str, bool, str | None]:
        saved = None
        if self.stream is not None:
            try:
                self.stream.flush()
                os.fsync(self.stream.fileno())
                self.stream.close()
                self.stream = None
                assert self.temporary is not None and self.target is not None
                os.replace(self.temporary, self.target)
                saved = str(self.target)
            except OSError as exc:
                self._failed("保存", exc)
        if self.characters <= self.limit:
            return self.raw_short if self.raw_short is not None else self.short, False, None
        half = self.limit // 2
        tail = self.tail[-half:]
        first_hidden = self.head.count("\n") + 1
        last_hidden = self.newlines - tail.count("\n") - int(self.tail[0] == "\n") + 1
        return output_preview(
            self.head, tail, characters=self.characters, limit=self.limit,
            hidden_lines=last_hidden - first_hidden + 1,
            total_lines=self.newlines + int(not self.tail.endswith("\n")), saved=saved,
        ), True, saved


def capture_command(command: str, workdir: Path, *, timeout_seconds: float,
                    stop_command: threading.Event | None, folder: Path, limit: int,
                    prune: Callable[[Path], None]) -> dict:
    if stop_command is not None and stop_command.is_set():
        return {"cancelled": True}
    started = time.monotonic()
    prepared = False
    prepare_lock = threading.Lock()

    def before_spill() -> None:
        nonlocal prepared
        with prepare_lock:
            if not prepared:
                prune(folder)
                prepared = True

    captures = {name: _Capture(folder, name, limit, before_spill) for name in ("stdout", "stderr")}
    # Attach before the shell runs so descendants remain owned even if it exits.
    process, job = start_command_process(command, cwd=workdir)
    read_failed = threading.Event()

    def consume(name: str) -> None:
        capture = captures[name]
        pipe = getattr(process, name)
        decoder, masker = _OutputDecoder(), _StreamMask()
        try:
            while True:
                data = pipe.read(8192)
                final = not data
                capture.feed(masker.feed(decoder.decode(data, final=final), final=final), final=final)
                if final:
                    break
        except Exception as exc:
            capture._failed("读取", exc)
            read_failed.set()
        finally:
            capture.masked = masker.masked_count
            try:
                pipe.close()
            finally:
                capture.result = capture.finish()

    readers = [threading.Thread(target=consume, args=(name,), daemon=True) for name in captures]
    timed_out = cancelled = False

    def kill() -> None:
        if job is not None:
            job.terminate_and_wait()
        else:
            import signal
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        try:
            process.kill()
        except OSError:
            pass

    try:
        for reader in readers:
            reader.start()
        while process.poll() is None or any(reader.is_alive() for reader in readers):
            cancelled = stop_command is not None and stop_command.is_set()
            timed_out = time.monotonic() - started >= timeout_seconds and not cancelled
            if cancelled or timed_out or read_failed.is_set():
                kill()
                break
            time.sleep(0.025)
    finally:
        try:
            if process.poll() is None:
                kill()
        finally:
            try:
                process.wait(timeout=10)
            finally:
                cleanup_command_script(process)
                try:
                    for reader in readers:
                        if reader.ident is not None:
                            reader.join(timeout=10)
                finally:
                    if job is not None:
                        if not cancelled and not timed_out and not read_failed.is_set():
                            job.release()
                        else:
                            job.close()
    if any(reader.is_alive() for reader in readers):
        raise RuntimeError("命令结束后输出管道仍未关闭")
    result = {"exit_code": process.returncode, "timed_out": timed_out, "cancelled": cancelled,
              "duration_ms": int((time.monotonic() - started) * 1000)}
    for name, capture in captures.items():
        assert capture.result is not None
        result[name], result[name + "_truncated"], saved = capture.result
        if saved is not None:
            result[name + "_file"] = saved
    errors = [capture.error for capture in captures.values() if capture.error]
    if errors:
        result["output_error"] = "；".join(errors)
    masked = sum(capture.masked for capture in captures.values())
    if masked:
        result.update(masked_secrets=masked, note=f"输出里有 {masked} 处像密钥的值已遮住，只留末 4 位。")
    return result

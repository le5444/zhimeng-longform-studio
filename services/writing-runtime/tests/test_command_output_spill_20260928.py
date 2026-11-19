"""命令输出太长时全文存成文件（9-28，台账 P1「命令输出全文落文件」，照 Crush / ZCode / Roo）。

以前一路输出超过 2.4 万字只留头尾各一半，中间那段直接丢了、模型再也看不到。现在头尾照给，
全文存进运行时文件夹下的 command-output/，中间那一行写明省了第几行到第几行、文件在哪，
模型用 read_file 从那一行接着读。存进去的全文也遮过密钥；7 天前的自动删。
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path
import pytest

from test_agent_direct_write_tools import _build_runtime, _call
from zhimeng_runtime.agent_tools import MAX_COMMAND_OUTPUT_CHARACTERS


def test_command_capture_memory_does_not_scale_with_both_complete_logs(tmp_path):
    import hashlib
    import tracemalloc
    from zhimeng_runtime.agent_tools import AgentToolExecutor, ComputerRunCommandArguments

    executor = AgentToolExecutor.__new__(AgentToolExecutor)
    executor._command_output_root = tmp_path / "command-output"
    script = tmp_path / "large.py"
    line = b"ordinary compilation progress " + b"x" * 220 + b"\n"
    count = 6000
    script.write_text(
        "import sys\n"
        f"line = {line!r}\n"
        f"for i in range({count}):\n"
        " sys.stdout.buffer.write(line)\n sys.stderr.buffer.write(line)\n",
        encoding="utf-8",
    )
    tracemalloc.start()
    try:
        output = executor._run_command(
            ComputerRunCommandArguments(command=f'"{sys.executable}" "{script}"', timeout_seconds=120), tmp_path,
        )
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert output["exit_code"] == 0
    expected = hashlib.sha256(line * count).hexdigest()
    for name in ("stdout", "stderr"):
        assert hashlib.sha256(Path(output[name + "_file"]).read_bytes()).hexdigest() == expected
    assert peak < 4 * 1024 * 1024, f"parent capture allocated {peak} bytes for two 1.5 MB logs"


def test_command_capture_reports_a_failed_log_save_and_keeps_draining(tmp_path, monkeypatch):
    from zhimeng_runtime.agent_tools import AgentToolExecutor, ComputerRunCommandArguments

    executor = AgentToolExecutor.__new__(AgentToolExecutor)
    executor._command_output_root = tmp_path / "command-output"
    original_open = Path.open
    def failing_spool(path, *args, **kwargs):
        if path.parent == executor._command_output_root and path.name.endswith(".tmp"):
            raise OSError("simulated disk full")
        return original_open(path, *args, **kwargs)
    monkeypatch.setattr(Path, "open", failing_spool)
    output = executor._run_command(
        ComputerRunCommandArguments(command=_printer(tmp_path, 3000), timeout_seconds=30), tmp_path,
    )
    assert output["exit_code"] == 0
    assert output["stdout_truncated"] is True
    assert "stdout_file" not in output
    assert output.get("output_error"), "missing complete log must not look like a successful save"
    assert "line 00001 " in output["stdout"] and "line 03000 " in output["stdout"]


@pytest.mark.parametrize("encoding", ["utf-8", "gbk"])
def test_both_streamed_logs_preserve_chinese_newlines_and_mask_split_values(tmp_path, encoding):
    from zhimeng_runtime.agent_tools import AgentToolExecutor, ComputerRunCommandArguments
    from zhimeng_runtime.secret_mask import mask_secrets

    executor = AgentToolExecutor.__new__(AgentToolExecutor)
    executor._command_output_root = tmp_path / "command-output"
    text = "开始，检查普通文件\r\n" + "编译记录: 123.45元\r\n" * 2500
    text += "OPENAI_API_KEY=sk-proj-SyntheticSecret_0123456789\r\n结束\r"
    expected, count = mask_secrets(text.replace("\r\n", "\n").replace("\r", "\n"))
    (tmp_path / "input.bin").write_bytes(text.encode(encoding))
    script = tmp_path / "both.py"
    script.write_text(
        "import os,sys\nfrom pathlib import Path\ndata=Path('input.bin').read_bytes()\n"
        "for i in range(0,len(data),101):\n"
        " os.write(sys.stdout.fileno(),data[i:i+101])\n os.write(sys.stderr.fileno(),data[i:i+101])\n",
        encoding="utf-8",
    )
    output = executor._run_command(ComputerRunCommandArguments(command=f'"{sys.executable}" "{script}"'), tmp_path)
    assert output["exit_code"] == 0
    assert output["masked_secrets"] == count * 2
    for name in ("stdout", "stderr"):
        assert Path(output[name + "_file"]).read_bytes() == expected.encode("utf-8")
        assert "SyntheticSecret" not in output[name]
    assert not list(executor._command_output_root.glob("*.tmp"))


def _printer(tmp_path: Path, lines: int, *, secret: str = "") -> str:
    script = tmp_path / "print_lines.py"
    script.write_text(
        "import sys\n"
        f"for i in range(1, {lines} + 1):\n"
        "    sys.stdout.write(f'line {i:05d} ' + 'x' * 40 + '\\n')\n"
        f"    if i == {lines // 2} and {secret!r}:\n"
        f"        sys.stdout.write({secret!r} + '\\n')\n",
        encoding="utf-8",
    )
    return f'"{sys.executable}" "{script}"'


def test_long_output_keeps_head_and_tail_and_saves_the_whole_output(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    command = _printer(tmp_path, 3000)

    result = asyncio.run(_call(executor, run, "computer_run_command", {"command": command}))

    output = result.output
    assert output["exit_code"] == 0
    assert output["stdout_truncated"] is True
    saved = Path(output["stdout_file"])
    assert saved.parent == tmp_path / "command-output"
    whole = saved.read_text(encoding="utf-8")
    assert whole.count("\n") == 3000
    assert "line 01500 " in whole
    shown = output["stdout"]
    assert shown.startswith("line 00001 ")
    assert shown.rstrip().endswith("x" * 40)
    assert "line 03000 " in shown
    assert "line 01500 " not in shown
    assert str(saved) in shown
    # 中间那一行（路径写了两遍：读的和搜的）之外，给模型的还是头尾各一半。
    assert len(shown) < MAX_COMMAND_OUTPUT_CHARACTERS + 800
    assert ("Select-String -Path" if os.name == "nt" else "grep -n") in shown
    assert "stderr_file" not in output

    # 每行 52 字：头部 12,000 字 = 230 整行 + 第 231 行的前半截，尾部从第 2770 行中间起，没列全的是第 231–2770 行。
    marker = next(line for line in shown.splitlines() if "中间省略" in line)
    assert "约 2540 行" in marker
    assert "全文 3000 行" in marker
    # 不点名起始行（考题里点名的那一行被模型当成了答案）。
    assert "start_line" not in marker
    # 存下的文件按 read_file 的数法数行：读第 1500 行就是第 1500 行。
    read = asyncio.run(
        _call(
            executor,
            run,
            "read_file",
            {"path": str(saved), "start_line": 1500, "max_lines": 1},
            call_id="call-0002",
        )
    )
    assert read.output["content"].splitlines()[0] == "line 01500 " + "x" * 40
    head_last = shown.split("\n…（中间省略")[0].splitlines()[-1]
    assert head_last == ("line 00231 " + "x" * 40)[: len(head_last)] and len(head_last) < 51


def test_short_output_is_not_saved(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)

    result = asyncio.run(_call(executor, run, "computer_run_command", {"command": _printer(tmp_path, 20)}))

    assert result.output["stdout_truncated"] is False
    assert "stdout_file" not in result.output
    assert not (tmp_path / "command-output").exists()


def test_saved_output_is_masked_too(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    secret = "OPENAI_API_KEY=sk-proj-abcdefghijklmnopqrstuvwx"

    result = asyncio.run(
        _call(executor, run, "computer_run_command", {"command": _printer(tmp_path, 3000, secret=secret)})
    )

    whole = Path(result.output["stdout_file"]).read_text(encoding="utf-8")
    assert "sk-proj-abcdefghijklmnopqrstuvwx" not in whole
    assert "OPENAI_API_KEY=" in whole
    assert result.output["masked_secrets"] == 1


def test_week_old_saved_outputs_are_cleaned_up(tmp_path: Path) -> None:
    executor, run, *_ = _build_runtime(tmp_path)
    folder = tmp_path / "command-output"
    folder.mkdir()
    old = folder / "cmd-20260901-000000-00000000-stdout.txt"
    recent = folder / "cmd-20260927-000000-11111111-stdout.txt"
    note = folder / "作者自己放的.txt"
    for path in (old, recent, note):
        path.write_text("旧输出\n", encoding="utf-8")
    eight_days_ago = time.time() - 8 * 24 * 3600
    os.utime(old, (eight_days_ago, eight_days_ago))
    os.utime(note, (eight_days_ago, eight_days_ago))

    asyncio.run(_call(executor, run, "computer_run_command", {"command": _printer(tmp_path, 3000)}))

    assert not old.exists()
    assert recent.exists()
    assert note.exists()  # 只清自己存的 cmd-*.txt

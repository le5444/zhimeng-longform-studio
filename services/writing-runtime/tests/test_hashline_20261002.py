"""Hashline 的最小可行版测试：
- read_file 给每行打 `行号#HASH:` 指纹，整份一个 file_tag。
- edit_file 的 find 没在最近一次 read_file 里出现过 → 拒绝，提示先读。
- 新建文件（existing is None）不查指纹。
"""
from __future__ import annotations

from zhimeng_runtime.hashline import file_tag, line_tags, tag_content


def test_file_tag_strips_trailing_whitespace_and_crlf() -> None:
    assert file_tag("a \n") == file_tag("a\n"), "行尾空格不影响指纹"
    assert file_tag("a\r\nb\r\n") == file_tag("a\nb\n"), "CRLF/LF 行尾不影响指纹"
    assert file_tag("hello\n") == file_tag("hello\r\n"), "同一行末尾 CRLF/LF 不影响指纹"
    tag = file_tag("hello\n")
    assert len(tag) == 4
    assert all(ch in "0123456789ABCDEF" for ch in tag), f"应该是 4 位大写 HEX：{tag}"


def test_line_tags_are_1_based_and_4_hex() -> None:
    tags = line_tags(["alpha\n", "beta \n", "\n", "gamma\r\n"])
    assert set(tags) == {1, 2, 3, 4}
    assert all(len(v) == 4 for v in tags.values())
    # 同内容的行，tag 一样（行尾空白不影响）
    assert line_tags(["x \n"])[1] == line_tags(["x\n"])[1]


def test_tag_content_prefixes_each_line() -> None:
    out = tag_content(["first\n", "second \n"], 5)
    lines = out.splitlines()
    assert len(lines) == 2
    assert lines[0].startswith("5#")
    assert lines[1].startswith("6#")
    assert ":first" in lines[0]


def test_edit_rejects_lines_not_read(tmp_path=None) -> None:
    # find 没在 read 的内容里出现 → 拦下
    tags = line_tags(["alpha\n", "beta\n", "gamma\n"])
    recent = set(tags.values())
    find_block = ["delta\n", "epsilon\n"]
    find_set = set(line_tags(find_block).values())
    assert not (find_set & recent), "delta/epsilon 不在 alpha/beta/gamma 的指纹集合里，应拒绝"


def test_edit_allows_lines_read(tmp_path=None) -> None:
    content = ["alpha\n", "beta\n", "gamma\n"]
    tags = line_tags(content)
    recent = set(tags.values())
    find_block = ["beta\n"]
    find_set = set(line_tags(find_block).values())
    assert find_set & recent, "beta 在读过的指纹集合里，应放行"


def test_evaluate_blocks_edits_after_file_changed_but_allows_if_unchanged() -> None:
    from zhimeng_runtime.hashline import LEDGER, ReadRecord, file_tag, line_tags

    rid = "run-1"
    path = "book.txt"
    content = ["a\n", "b\n"]
    LEDGER.clear_run(rid)
    # 读过
    LEDGER.record(rid, path, ReadRecord(file_tag=file_tag("a\nb\n"), line_tags=line_tags(content), excerpt_lines={1, 2}))
    # 文件没变：放行
    allowed, msg = LEDGER.evaluate(rid, path, file_tag("a\nb\n"))
    assert allowed and msg is None
    # 文件变了：拒绝
    allowed2, msg2 = LEDGER.evaluate(rid, path, file_tag("a\nc\n"))
    assert not allowed2 and "read_file" in (msg2 or "")
    # 新建的、没读过的文件：放行
    LEDGER.clear_run(rid)
    allowed3, _ = LEDGER.evaluate(rid, "newfile.txt", file_tag("x\n"))
    assert allowed3

from __future__ import annotations

"""Stage the executable core of the four writing Skills for packaging.

The source Skills live outside this repository during development.  This
command copies only their executable Python code, contracts/references, and a
small distillation card index into a reviewable bundle.  Raw downloaded books,
cache files, backups, and provider credentials are deliberately excluded.
"""

import argparse
import hashlib
import json
import re
import shutil
from datetime import UTC, datetime
from pathlib import Path


EXCLUDED_NAMES = {"__pycache__", ".pytest_cache", ".git", "raw", "backups"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sanitize_text(text: str) -> str:
    return re.sub(
        r"(?i)(?:[A-Z]:)?[\\/]+Users[\\/]+demo",
        "<author-local-path>",
        text,
    )


def _rewrite_portable_kb_manager(text: str) -> str:
    """Remove the CLI's development-machine defaults in the staged copy."""
    replacements = {
        "DEFAULT_DISTILLATION_LIBRARY": 'SKILLS_ROOT / "novel-distillation" / "library"',
        "DEFAULT_DOWNLOAD_DIR": "Path.cwd() / \"downloaded-sources\"",
        "DEFAULT_EXTERNAL_REFERENCE_WORKSPACE": 'SKILL_ROOT / "references" / "external-reference-workspace"',
        "DISTILL_KB_SCRIPT": 'SKILLS_ROOT / "novel-distillation" / "scripts" / "distill_kb.py"',
        "DISTILL_KB_LIBRARY": 'SKILLS_ROOT / "novel-distillation" / "library"',
    }
    for name, expression in replacements.items():
        text = re.sub(
            rf"^{name} = .*?$",
            f"{name} = {expression}",
            text,
            flags=re.MULTILINE,
        )
    text = re.sub(
        r"CREATION_SUITE_DISTILLATION_BRIDGE = Path\(.*?\)",
        'CREATION_SUITE_DISTILLATION_BRIDGE = SKILLS_ROOT / "novel-creation-suite" / "references" / "bundled-skills" / "novel-suite-orchestrator" / "scripts" / "build_distillation_context.py"',
        text,
        flags=re.DOTALL,
    )
    text = re.sub(
        r'"script": r"[^"]*validate_draft_body_cleanliness\.py",',
        '"script": str(SKILLS_ROOT / "novel-creation-suite" / "scripts" / "validate_draft_body_cleanliness.py"),',
        text,
    )
    return text


def _copy_file(
    source: Path,
    destination: Path,
    *,
    sanitize_text: bool = False,
    sanitize_code: bool = False,
    portable_kb_manager: bool = False,
) -> None:
    if not source.is_file() or source.is_symlink():
        raise RuntimeError(f"source file is unavailable or linked: {source}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    if sanitize_code and source.suffix.lower() in {".py", ".ps1"}:
        # 脚本里的开发机路径只出现在参数默认值上（$DownloadDir、$DownloaderScript
        # 等），Runtime 调用时一律显式传参，所以替换默认值不影响功能——但把作者的
        # 真实路径打进分发包不行。逐字节读写：这些 .ps1 是 CRLF/LF 混合的，
        # read_text/write_text 会把行尾悄悄压平。
        raw = source.read_bytes().decode("utf-8", errors="replace")
        destination.write_bytes(_sanitize_text(raw).encode("utf-8"))
        return
    if sanitize_text and source.suffix.lower() in {".md", ".markdown", ".json", ".jsonl", ".txt"}:
        text = source.read_text(encoding="utf-8", errors="replace")
        if portable_kb_manager:
            text = _rewrite_portable_kb_manager(text)
        destination.write_text(_sanitize_text(text), encoding="utf-8")
    elif portable_kb_manager:
        text = _rewrite_portable_kb_manager(
            source.read_text(encoding="utf-8", errors="replace")
        )
        destination.write_text(_sanitize_text(text), encoding="utf-8")
    else:
        shutil.copy2(source, destination)


def _copy_tree(
    source: Path,
    destination: Path,
    *,
    suffixes: set[str] | None = None,
    sanitize_text: bool = False,
    sanitize_code: bool = False,
    portable_kb_manager: bool = False,
) -> None:
    if not source.is_dir() or source.is_symlink():
        raise RuntimeError(f"source directory is unavailable or linked: {source}")
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part in EXCLUDED_NAMES for part in relative.parts):
            continue
        if not path.is_file() or path.is_symlink():
            continue
        if suffixes is not None and path.suffix.lower() not in suffixes:
            continue
        if ".bak" in path.name.lower() or path.name.endswith(".pyc"):
            continue
        _copy_file(
            path,
            destination / relative,
            sanitize_text=sanitize_text,
            sanitize_code=sanitize_code,
            portable_kb_manager=portable_kb_manager,
        )


def _copy_library_index(source: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    for name in ("cards.jsonl", "feedback.jsonl", "manifest.json"):
        candidate = source / name
        if candidate.is_file() and not candidate.is_symlink():
            _copy_file(candidate, destination / name, sanitize_text=True)
    # Keep the topology expected by the CLIs without shipping raw novels.
    for name in ("cards", "reports", "raw"):
        (destination / name).mkdir(exist_ok=True)


def _copy_kb_layer_libraries(source: Path, destination: Path) -> int:
    """把 kb-manager 的分层库（domain/style/platform/ops）真正拷进随包快照。

    以前这里只 `mkdir` 出空目录，于是随包的 `kb_manager list-libraries` 返回
    四个空数组，`assemble-context` 换台电脑必然失败——而状态面板还报
    「换电脑不依赖本机 .codex」。空骨架比缺失更坏，因为它看起来是好的。

    只拷分层库（260 个文件 / 约 1.1 MB 的机制卡与规则），**不拷 `projects/`**：
    那是作者自己的作品知识库（123 MB），属于个人内容，不进分发包。
    """
    layers_source = source / "global"
    if not layers_source.is_dir():
        return 0
    copied = 0
    for layer in ("domain", "style", "platform", "ops"):
        layer_source = layers_source / layer
        if not layer_source.is_dir():
            continue
        _copy_tree(
            layer_source,
            destination / "global" / layer,
            sanitize_text=True,
        )
        copied += sum(1 for path in layer_source.rglob("*") if path.is_file())
    # 作者作品库与报告不随包；建出空目录让 CLI 的路径假设成立。
    for name in ("projects", "reports"):
        (destination / name).mkdir(parents=True, exist_ok=True)
    manifest = source / "manifest.json"
    if manifest.is_file() and not manifest.is_symlink():
        _copy_file(manifest, destination / "manifest.json", sanitize_text=True)
    return copied


def stage(*, skills_root: Path, output: Path) -> dict[str, object]:
    skills_root = skills_root.expanduser().resolve(strict=True)
    if not skills_root.is_dir():
        raise RuntimeError(f"Skills root is not a directory: {skills_root}")
    if output.exists():
        raise RuntimeError(f"output already exists; remove it explicitly first: {output}")
    output.mkdir(parents=True, exist_ok=False)
    try:
        distillation = skills_root / "novel-distillation"
        kb_manager = skills_root / "novel-kb-manager"
        creation = skills_root / "novel-creation-suite"
        tomato = skills_root / "tomato-novel-auto-distill"
        for root in (distillation, kb_manager, creation, tomato):
            if not root.is_dir():
                raise RuntimeError(f"required Skill directory is missing: {root}")

        _copy_tree(distillation / "scripts", output / "novel-distillation" / "scripts", suffixes={".py"})
        _copy_library_index(distillation / "library", output / "novel-distillation" / "library")

        _copy_file(
            kb_manager / "scripts" / "kb_manager.py",
            output / "novel-kb-manager" / "scripts" / "kb_manager.py",
            portable_kb_manager=True,
        )
        _copy_tree(
            kb_manager / "references",
            output / "novel-kb-manager" / "references",
            sanitize_text=True,
        )
        layer_card_files = _copy_kb_layer_libraries(
            kb_manager / "library",
            output / "novel-kb-manager" / "library",
        )
        if layer_card_files == 0:
            raise RuntimeError(
                "novel-kb-manager 的分层知识库是空的："
                f"{kb_manager / 'library' / 'global'} 下没有可随包的 domain/style/platform/ops 卡。"
                " 随包快照不能只带空目录——那会让状态面板谎报 portable_ready。"
            )

        _copy_tree(creation / "scripts", output / "novel-creation-suite" / "scripts", suffixes={".py"})
        _copy_tree(
            creation / "references",
            output / "novel-creation-suite" / "references",
            sanitize_text=True,
        )

        # 第四套：来源导入。以前这个脚本只 staging 三套，于是随包快照永远缺
        # tomato，作者在干净机上"输入小说名称导入来源"必然失败。
        # PowerShellTomatoSkillAdapter 以 strict=True 硬要求
        # scripts/prepare_tomato_sources.ps1 与 scripts/extract_epub_text.py，
        # 并按 skill_root.parent / "novel-distillation" 找蒸馏脚本——所以这一套
        # 必须与另外三套平级 staging。第三方下载器（-DownloaderScript）不随包：
        # 它是外部工具，由作者在 runtime.env 里自行指定。
        _copy_tree(
            tomato / "scripts",
            output / "tomato-novel-auto-distill" / "scripts",
            suffixes={".py", ".ps1"},
            sanitize_text=True,
            sanitize_code=True,
        )
        _copy_file(
            tomato / "SKILL.md",
            output / "tomato-novel-auto-distill" / "SKILL.md",
            sanitize_text=True,
        )
        for required in (
            output / "tomato-novel-auto-distill" / "scripts" / "prepare_tomato_sources.ps1",
            output / "tomato-novel-auto-distill" / "scripts" / "extract_epub_text.py",
        ):
            if not required.is_file():
                raise RuntimeError(
                    "tomato 随包快照缺少 Runtime 强依赖的入口脚本："
                    f"{required.name}。带着空目录发包，状态面板会谎报可用。"
                )

        files = []
        for path in sorted(output.rglob("*")):
            if path.is_file():
                files.append(
                    {
                        "path": path.relative_to(output).as_posix(),
                        "size_bytes": path.stat().st_size,
                        "sha256": _sha256(path),
                    }
                )
        material = json.dumps(files, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        manifest = {
            "schema": "zhimeng.four-skills-runtime-bundle.v1",
            "created_at": datetime.now(UTC).isoformat(),
            # Do not put the developer's absolute .codex path into a bundle
            # that may be copied to another computer or shared for review.
            "source_root": "external-read-only-skills-root",
            "source_root_path_recorded": False,
            "includes_novel_body": False,
            "includes_provider_credentials": False,
            "files": files,
            "file_count": len(files),
            "bundle_hash": hashlib.sha256(material.encode("utf-8")).hexdigest(),
        }
        (output / "BUNDLE-MANIFEST.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        return manifest
    except Exception:
        shutil.rmtree(output, ignore_errors=True)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skills-root",
        type=Path,
        default=Path.home() / ".codex" / "skills",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    manifest = stage(skills_root=args.skills_root, output=args.output)
    print(
        json.dumps(
            {
                "output": str(args.output.resolve()),
                "file_count": manifest["file_count"],
                "bundle_hash": manifest["bundle_hash"],
                "includes_novel_body": False,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

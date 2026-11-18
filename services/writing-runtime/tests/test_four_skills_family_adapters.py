from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
ADAPTERS = {
    "source_ingest": ROOT / "adapters" / "builtin" / "four-skills-family-source-ingest" / "run_family_step.py",
    "distillation": ROOT / "adapters" / "builtin" / "four-skills-family-distillation" / "run_family_step.py",
    "knowledge_context": ROOT / "adapters" / "builtin" / "four-skills-family-knowledge-context" / "run_family_step.py",
    "creation_suite": ROOT / "adapters" / "builtin" / "four-skills-family-creation-suite" / "run_family_step.py",
}


def _write_toolchain(tmp_path: Path) -> dict[str, Path]:
    source = tmp_path / "fake_source.py"
    source.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "out = Path(sys.argv[sys.argv.index('--out-dir') + 1])\n"
        "out.mkdir(parents=True, exist_ok=True)\n"
        "print(json.dumps({'status': 'draft_ready'}))\n",
        encoding="utf-8",
    )
    distillation = tmp_path / "fake_distillation.py"
    distillation.write_text(
        "import json\n"
        "print(json.dumps({'cards': 3, 'health': 'healthy', 'structural_health': 'healthy'}))\n",
        encoding="utf-8",
    )
    knowledge = tmp_path / "fake_kb_manager.py"
    knowledge.write_text(
        "import json\n"
        "print(json.dumps({'domain': ['fixture.kb']}))\n",
        encoding="utf-8",
    )
    creation = tmp_path / "fake_creation.py"
    creation.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "out = Path(sys.argv[sys.argv.index('--output') + 1])\n"
        "out.parent.mkdir(parents=True, exist_ok=True)\n"
        "out.write_text(json.dumps({'content_hash': 'a' * 64, 'overall_score': {'score': 4.2, 'level': 'low'}}), encoding='utf-8')\n"
        "print('{}')\n",
        encoding="utf-8",
    )
    library = tmp_path / "library"
    library.mkdir()
    return {
        "source": source,
        "distillation": distillation,
        "knowledge": knowledge,
        "creation": creation,
        "library": library,
    }


def _run_adapter(
    tmp_path: Path,
    family: str,
    toolchain: dict[str, Path],
    *,
    invalid_script: bool = False,
    bundle_root: Path | None = None,
) -> tuple[subprocess.CompletedProcess[str], dict, dict]:
    snapshot = tmp_path / "snapshot"
    staging = tmp_path / "staging"
    snapshot.mkdir()
    staging.mkdir()
    (snapshot / "chapter.md").write_text("第一章。旧门没有锁。\n", encoding="utf-8")
    env = os.environ.copy()
    env.pop("ZHIMENG_FOUR_SKILLS_SOURCE_INGEST_SCRIPT", None)
    env.pop("ZHIMENG_FOUR_SKILLS_DISTILLATION_SCRIPT", None)
    env.pop("ZHIMENG_FOUR_SKILLS_DISTILLATION_LIBRARY", None)
    env.pop("ZHIMENG_FOUR_SKILLS_KB_MANAGER_SCRIPT", None)
    env.pop("ZHIMENG_FOUR_SKILLS_KB_LIBRARY", None)
    env.pop("ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT", None)
    env.pop("ZHIMENG_FOUR_SKILLS_CREATION_LIBRARY", None)
    env.pop("ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT", None)
    if family == "source_ingest":
        env["ZHIMENG_FOUR_SKILLS_SOURCE_INGEST_SCRIPT"] = str(
            tmp_path / "missing.py" if invalid_script else toolchain["source"]
        )
    elif family == "distillation":
        env["ZHIMENG_FOUR_SKILLS_DISTILLATION_SCRIPT"] = str(
            tmp_path / "missing.py" if invalid_script else toolchain["distillation"]
        )
        env["ZHIMENG_FOUR_SKILLS_DISTILLATION_LIBRARY"] = str(toolchain["library"])
    elif family == "knowledge_context":
        env["ZHIMENG_FOUR_SKILLS_KB_MANAGER_SCRIPT"] = str(toolchain["knowledge"])
        env["ZHIMENG_FOUR_SKILLS_KB_LIBRARY"] = str(toolchain["library"])
    else:
        env["ZHIMENG_FOUR_SKILLS_CREATION_SCRIPT"] = str(toolchain["creation"])
        env["ZHIMENG_FOUR_SKILLS_CREATION_LIBRARY"] = str(toolchain["library"])
    if bundle_root is not None:
        env["ZHIMENG_FOUR_SKILLS_BUNDLE_ROOT"] = str(bundle_root)
    request = {
        "schema": "zhimeng.four-skills-family-step-request.v1",
        "family": family,
        "batch_id": "batch-adapter-test",
        "probe_id": "probe-adapter-test",
    }
    completed = subprocess.run(
        [sys.executable, str(ADAPTERS[family]), str(snapshot)],
        cwd=staging,
        env={**env, "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1"},
        input=json.dumps(request, ensure_ascii=False),
        text=True,
        # 与 test_draft_gate_adapter.py 同一处理：生产路径
        # （skill_executor.py 走 bytes + 显式 encode("utf-8")）不受终端影响，
        # 但 text=True 默认按系统 ANSI 代码页编码 stdin（本机是 gbk）。
        # 外部设了 PYTHONIOENCODING=utf-8 跑 pytest 时两边就打架，中文请求会
        # UnicodeDecodeError，表面报成「JSONDecodeError: Expecting value」，
        # 看着像代码回归其实是环境陷阱（2026-09-12 实测：prewrite 5 红，去掉前缀全绿）。
        encoding="utf-8",
        capture_output=True,
        check=False,
        timeout=20,
    )
    assert completed.stdout, completed.stderr
    envelope = json.loads(completed.stdout)
    artifact = json.loads((staging / "family-step-result.json").read_text(encoding="utf-8"))
    return completed, envelope, artifact


def _stage_fake_bundle(tmp_path: Path, toolchain: dict[str, Path]) -> Path:
    bundle = tmp_path / "four-skills-runtime"
    mapping = {
        "source": "novel-distillation/scripts/distill_source_packet.py",
        "distillation": "novel-distillation/scripts/distill_kb.py",
        "knowledge": "novel-kb-manager/scripts/kb_manager.py",
        "creation": "novel-creation-suite/scripts/detect_ai_smell.py",
    }
    for key, relative in mapping.items():
        destination = bundle / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(toolchain[key], destination)
    (bundle / "novel-distillation/library").mkdir(parents=True, exist_ok=True)
    (bundle / "novel-kb-manager/library").mkdir(parents=True, exist_ok=True)
    return bundle


@pytest.mark.parametrize("family", sorted(ADAPTERS))
def test_family_adapter_calls_configured_cli_and_records_tool_evidence(
    tmp_path: Path,
    family: str,
) -> None:
    completed, envelope, artifact = _run_adapter(tmp_path, family, _write_toolchain(tmp_path))

    assert completed.returncode == 0, completed.stderr
    assert envelope["health"] == "healthy"
    assert envelope["result"]["executed"] is True
    assert envelope["result"]["writes_project_truth"] is False
    assert artifact["adapter_status"] == "succeeded"
    assert artifact["tool"]["status"] == "succeeded"
    assert artifact["tool"]["exit_code"] == 0


def test_family_adapter_reports_unavailable_tool_instead_of_fake_success(
    tmp_path: Path,
) -> None:
    completed, envelope, artifact = _run_adapter(
        tmp_path,
        "distillation",
        _write_toolchain(tmp_path),
        invalid_script=True,
    )

    assert completed.returncode == 1
    assert envelope["health"] == "blocked"
    assert artifact["adapter_status"] == "failed"
    assert artifact["tool"]["failure_code"] == "tool_unavailable"


@pytest.mark.parametrize("family", sorted(ADAPTERS))
def test_family_adapter_prefers_bundled_toolchain_over_configured_paths(
    tmp_path: Path,
    family: str,
) -> None:
    toolchain = _write_toolchain(tmp_path)
    bundle = _stage_fake_bundle(tmp_path, toolchain)
    completed, envelope, artifact = _run_adapter(
        tmp_path,
        family,
        toolchain,
        bundle_root=bundle,
    )

    assert completed.returncode == 0, completed.stderr
    assert envelope["health"] == "healthy"
    assert artifact["tool"]["source"] == "bundled"
    assert str(bundle) in artifact["tool"]["script"]

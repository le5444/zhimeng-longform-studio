#!/usr/bin/env python3
"""Audit a novel project scaffold and suggest the next workflow route."""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import re
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8")


STANDARD_DIRS = [
    "01_项目定位",
    "02_世界观设定",
    "03_人物设定",
    "04_剧情大纲",
    "05_正文内容",
    "06_写作规范",
    "07_运营数据",
    "08_素材库",
    "09_完本复盘",
]

LEGACY_OR_RISKY_DIRS = [
    "06_正文",
    "正文",
    "正文内容",
    "大纲",
    "设定",
    "人物",
    "世界观",
]

# 历史导出、备份和镜像不是当前小说真值层；审计时跳过，避免旧问题永久污染当前健康状态。
EXCLUDED_SCAN_DIR_SEQUENCES = [
    ("10_导出文件", "备份"),
    ("10_导出文件", "历史"),
    ("10_导出文件", "archive"),
    ("10_导出文件", "archives"),
]


KEY_STATE_FILES = [
    "03_人物设定/人物状态跟踪表.md",
    "03_人物设定/人物关系网.md",
    "04_剧情大纲/章节细纲.md",
    "04_剧情大纲/伏笔埋设与回收.md",
    "05_正文内容/正文总览.md",
    "05_正文内容/每日写作进度表.md",
    "05_正文内容/修改记录.md",
    "07_运营数据/追读设计.md",
]

CHAPTER_RE = re.compile(r"(^|\D)(\d{1,4})(?:[-_第章\s]|$)")
CHAPTER_TEXT_PATTERNS = [
    re.compile(r"CH0*(\d{1,4})", re.IGNORECASE),
    re.compile(r"第0*(\d{1,4})章"),
    re.compile(r"(^|\D)0*(\d{1,4})(?:[-_第章\s]|$)"),
]
CHAPTER_VERSION_MARKERS = {
    "control_card": ["控制卡"],
    "draft": ["初稿", "风险草稿"],
    "deai": ["去模板化稿", "去AI稿", "去AI化稿"],
    "review": ["审稿报告"],
    "final": ["定稿"],
    "writeback": ["回填记录"],
}

HANDOFF_CONTINUATION_REQUIREMENTS = {
    "control_card": ["continuation_state_contract"],
    "draft": ["Continuation State Draft Evidence Plan", "continuation_state_not_consumed"],
    "deai": ["Continuation State Draft Evidence Plan", "continuation_state_not_consumed"],
    "review": ["continuation_state_consumption", "continuation_state_not_consumed"],
    "final": ["Continuation State Draft Evidence Plan", "continuation_state_not_consumed"],
}

FORMAL_CONTINUATION_TRIGGER_TERMS = [
    "continuation_state",
    "state_freshness",
    "chapter_call_list",
    "novel-kb-manager",
    "NLCL",
]

FORMAL_CONTINUATION_REQUIREMENTS = {
    "control_card": ["continuation_state_contract"],
    "review": ["continuation_state_consumption"],
}

NODE_ACCEPTANCE_TRIGGER_TERMS = [
    "execution_pack",
    "node_acceptance_gate",
    "节点验收门",
]

NODE_ACCEPTANCE_REQUIRED_TERMS = [
    "node_acceptance_gate",
    "readability_check",
    "sentence_mix_check",
    "dialogue_pressure_check",
    "psychology_externalization_check",
    "scene_change_check",
]

SYSTEM_PANEL_STRONG_TRIGGER_TERMS = [
    "系统流",
    "系统面板",
    "任务栏",
    "属性栏",
    "属性面板",
    "奖励结算",
    "资源账本",
    "面板空转",
    "system_panel_without_real_world_effect",
]

SYSTEM_PANEL_WEAK_TRIGGER_TERMS = [
    "返利",
    "任务奖励",
    "奖励到账",
    "金手指",
]

SYSTEM_PANEL_CONTRACT_ALIASES = [
    "system_panel_effect_contract",
    "systemPanelEffectContract",
]

SYSTEM_PANEL_REQUIRED_TERMS = [
    "panel_text",
    "trigger_condition",
    "active_probe",
    "first_real_world_effect",
    "external_witness_or_pressure",
    "resource_or_status_delta",
    "new_cost_or_debt",
    "reader_verification_window",
]

INFORMATION_REVEAL_TRIGGER_TERMS = [
    "信息章",
    "线索章",
    "调查章",
    "推理章",
    "秘密揭示",
    "真相揭示",
    "设定解释",
    "解释设定",
    "NPC解释",
    "NPC 解释",
    "免费答案",
    "主角得到答案",
    "得到线索",
    "获得线索",
    "发现异常",
    "信息揭露",
    "information_reveal_without_friction",
]

INFORMATION_REVEAL_CONTRACT_ALIASES = [
    "information_reveal_contract",
    "informationRevealContract",
]

INFORMATION_REVEAL_REQUIRED_TERMS = [
    "question_or_secret",
    "who_wants_answer",
    "withholder_or_obstacle",
    "wrong_lead_or_misread",
    "trace_or_object_anchor",
    "cost_or_consequence",
    "partial_truth_now",
    "delayed_truth_boundary",
]

XIANXIA_XUANHUAN_TRIGGER_TERMS = [
    "修仙",
    "仙侠",
    "玄幻",
    "东方玄幻",
    "凡人修仙",
    "宗门",
    "世家",
    "王朝",
    "散修",
    "灵根",
    "血脉",
    "体质",
    "功法",
    "丹药",
    "法宝",
    "秘境",
    "试炼",
    "渡劫",
    "境界",
    "越级挑战",
    "xianxia_xuanhuan_contract_missing",
    "xianxia_xuanhuan_contract_not_consumed",
]

XIANXIA_XUANHUAN_CONTRACT_ALIASES = [
    "xianxia_xuanhuan_chapter_contract",
    "xianxiaXuanhuanChapterContract",
]

XIANXIA_XUANHUAN_REQUIRED_TERMS = [
    "realm_state",
    "resource_ledger",
    "sect_or_rule_pressure",
    "technique_cost_limit",
    "visible_trial_or_conflict",
    "earned_payoff",
    "status_delta",
    "future_debt",
]

PROMPT_GOAL_BUDGET_TRIGGER_TERMS = [
    "外部提示词",
    "长提示词",
    "提示词清单",
    "提示词压缩",
    "提示词不是死命令",
    "网站模板",
    "禁用词",
    "禁用词清单",
    "润色清单",
    "死命令",
    "prompt_list_dead_command_overload",
    "prompt_goal_budget",
]

PROMPT_GOAL_BUDGET_REQUIRED_TERMS = [
    "prompt_goal_budget",
    "source_prompt_summary",
    "accepted_goals",
    "rejected_or_deferred_rules",
    "conflict_resolution",
    "acceptance_tests",
]

STORY_TRACKER_DELTA_REQUIRED_TERMS = [
    "story_tracker_delta",
    "state_change",
    "relationship_delta",
    "resource_delta",
    "debt_delta",
    "map_or_authority_delta",
    "next_interface",
]

APPEND_ONLY_TIMELINE_TRIGGER_TERMS = [
    "append_only_timeline",
    "时间线",
    "timeline",
    "open debt",
    "open debts",
    "open_debts",
    "叙事债务",
    "伏笔生命周期",
    "地图权限",
    "map_or_authority",
    "map_or_authority_delta",
    "系统状态",
    "公开认知",
    "public cognition",
]

APPEND_ONLY_TIMELINE_REQUIRED_TERMS = [
    "append_only_timeline",
    "delta_added",
    "correction_note",
    "replaced_by",
    "reason",
    "overwrite_without_trace",
]

NLCL_ADVISORY_TRIGGER_TERMS = [
    "nlcl_advisory_gates",
    "advisory_yellow_light",
    "advisory_missing_fields",
]

EXTERNAL_REFERENCE_CLOSURE_TRIGGER_TERMS = [
    "external_reference_coverage_closure",
    "source_pool_closure_status",
]
EXTERNAL_REFERENCE_CLOSURE_WARNING_STATUSES = {
    "missing",
    "stale_snapshot",
    "needs_followup_assets",
    "not_provided",
}

NLCL_ADVISORY_FIELD_REQUIRED_TERMS = {
    "chapter_function_ledger": [
        "event_change",
        "emotional_turn",
        "debt_movement",
        "resource_or_state_delta",
        "end_hook",
    ],
    "hook_variety_ledger": [
        "hook_type",
        "reversal_type",
        "last_used_chapter",
        "debt_link",
    ],
    "continuity_preflight_gate": [
        "continuity_preflight_gate",
        "称谓",
        "时间线",
        "人物状态",
        "未偿债务",
        "禁止提前揭示",
    ],
    "deai_scope": [
        "deai_scope",
        "事实",
        "视角",
        "顺序",
        "禁改事实",
        "目标长度",
    ],
}

REVISION_PLATEAU_ACTION_TERMS = [
    "修稿",
    "返工",
    "重写",
    "去AI",
    "去 AI",
    "去模板化",
    "审稿",
    "低分",
    "润色",
]

REVISION_PLATEAU_STRONG_TRIGGER_TERMS = [
    "多轮",
    "反复",
    "循环",
    "连续两轮",
    "连续 2 轮",
    "第二轮",
    "第三轮",
    "分数不涨",
    "不升分",
    "停滞",
    "平台期",
    "revision_plateau",
    "top_issues",
    "before_score",
    "after_score",
    "changed_spans",
    "remaining_blockers",
    "cold_read",
    "冷读",
    "刷指标",
    "检测器",
]

REVISION_PLATEAU_REQUIRED_TERMS = [
    "top_issues",
    "before_score",
    "after_score",
    "changed_spans",
    "remaining_blockers",
]

REVISION_PLATEAU_EXPLICIT_TERMS = [
    "平台期",
    "revision_plateau",
    "分数不涨",
    "不升分",
    "停滞",
]

REVISION_PLATEAU_REROUTE_TERMS = [
    "L2_CONTROL_CARD",
    "L4_DRAFT_EXECUTION",
    "L5_DEAI_STYLE",
    "L6_REVIEW_ACCEPTANCE",
    "冷读",
    "人工复核",
    "cold_read",
    "human_review",
    "novel-score-improver",
    "novel-chapter-reviewer",
    "upstream_replan",
]

AI_SMELL_RISK_PROBES = [
    "compressed_fragment_probe",
    "semantic_completeness_probe",
    "report_style_probe",
    "natural_order_probe",
    "mental_summary_probe",
    "dialogue_pressure_probe",
]
AI_SMELL_BLOCKING_LEVELS = {"medium", "high"}
AI_SMELL_FINAL_MARKERS = ["定稿", "发布稿", "交付稿", "final"]
AI_SMELL_INTERMEDIATE_MARKERS = ["初稿", "去AI稿", "去模板化稿", "去模板化", "风险草稿", "draft"]
AI_SMELL_BRIDGE_TRIGGER_TERMS = [
    "ai_smell_project_audit_bridge",
    "detect_ai_smell.py",
    "*_ai_smell_report.json",
    "*_compare_ai_smell_report.json",
]
AI_SMELL_BRIDGE_PRESENT_TERMS = [
    "--ai-smell-report-dir",
    "ai_smell_reports_seen",
]
LONG_FORM_READABILITY_MIN_CHARS = 1800
LONG_FORM_READABILITY_EVIDENCE_TERMS = [
    "detect_ai_smell.py",
    "ai_smell_project_audit_bridge",
    "short_fragment_metrics",
    "compressed_fragment_probe",
    "semantic_completeness_probe",
    "natural_order_probe",
    "deai_drift",
    "真网文顺读",
    "2000字顺读",
    "自然语序",
    "冷读",
    "短碎",
    "语义残缺",
    "生硬直白",
]
LONG_FORM_NON_MANUSCRIPT_MARKERS = [
    "控制卡",
    "初稿",
    "风险草稿",
    "去模板化稿",
    "去AI稿",
    "审稿",
    "报告",
    "问题卡",
    "回填",
    "修改记录",
    "README",
]
LONG_FORM_REVIEW_GATE_REQUIRED_TERMS = [
    "reader_cost_report",
    "human_friction_surface_pass",
    "readability_score_cap",
]

DIRECT_DRAFT_TRIGGER_TERMS = [
    "AI直出",
    "ai直出",
    "直出",
    "direct_draft",
    "risk_draft",
    "风险草稿",
    "draft_done",
    "读通顺",
    "读得通顺",
    "剧情通顺",
    "语句通顺",
    "通顺就可以",
]
DIRECT_DRAFT_FINAL_CLAIM_TERMS = [
    "approved",
    "finalized",
    "writeback_done",
    "release_ready",
    "可发布",
    "可定稿",
    "可以定稿",
    "已定稿",
    "定稿通过",
    "定稿完成",
    "发布稿",
    "交付稿",
    "完整闭环",
    "可进入发布",
]
DIRECT_DRAFT_EVIDENCE_TERMS = [
    "控制卡",
    "control_card",
    "chapter_call_list",
    "去AI",
    "去 AI",
    "去模板化",
    "deai_done",
    "novel-style-deai",
    "章节审稿",
    "审稿报告",
    "chapter_review",
    "novel-chapter-reviewer",
    "human_judgment_gate",
    "人工冷读",
    "冷读",
    "回填",
    "回填完成",
    "回填记录",
    "facts_delta",
]

LOW_READ_PACKAGING_TERMS = ["低阅读", "点击低", "首日数据弱", "曝光转化弱", "没人看"]
PACKAGING_SIGNAL_DIRS = {"01_项目定位", "07_运营数据", "09_完本复盘"}
PACKAGING_SIGNAL_NAME_MARKERS = [
    "包装",
    "运营",
    "读者反馈",
    "数据",
    "审稿",
    "评分",
    "问题卡",
    "压力测试",
    "报告",
    "复盘",
]
PACKAGING_TRIAGE_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_packaging_triage.py"
)
UNIFIED_ISSUE_CARD_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_unified_issue_card.py"
)
AI_SMELL_BRIDGE_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_ai_smell_bridge.py"
)
PROMPT_GOAL_BUDGET_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_prompt_goal_budget.py"
)
CONTROL_CARD_CONTRACT_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_control_card_contract.py"
)
REVIEW_ACCEPTANCE_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "validate_review_acceptance.py"
)
CONTINUATION_CHECK_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "check_continuation_state_consumption.py"
)
DETECT_AI_SMELL_SCRIPT = (
    Path(__file__).resolve().parents[4]
    / "scripts"
    / "detect_ai_smell.py"
)




def is_excluded_scan_path(path: Path, root: Path) -> bool:
    """Return True for archived/export backup files that should not affect current health."""
    try:
        rel_parts = path.relative_to(root).parts
    except ValueError:
        rel_parts = path.parts
    lowered = tuple(str(part).lower() for part in rel_parts)
    for seq in EXCLUDED_SCAN_DIR_SEQUENCES:
        seq_lower = tuple(part.lower() for part in seq)
        if len(seq_lower) > len(lowered):
            continue
        for idx in range(0, len(lowered) - len(seq_lower) + 1):
            if lowered[idx: idx + len(seq_lower)] == seq_lower:
                return True
    return False


def active_project_files(root: Path, pattern: str) -> list[Path]:
    return [path for path in root.rglob(pattern) if not is_excluded_scan_path(path, root)]


def load_expected_files() -> list[tuple[str, str, str]]:
    init_script = Path(__file__).with_name("init_novel_project.py")
    tree = ast.parse(init_script.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "FILES":
                    value = ast.literal_eval(node.value)
                    return [(str(path), str(heading), str(skill)) for path, heading, skill in value]
    raise RuntimeError("Could not find FILES in init_novel_project.py")


@dataclass
class Finding:
    severity: str
    code: str
    path: str
    message: str
    route: str
    details: dict | None = None


def read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        return path.read_text(encoding="utf-8", errors="replace")


def is_placeholder(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    markers = ["状态：待生成", "待生成。", "TODO", "TBD", "占位"]
    return any(marker in stripped for marker in markers) and len(stripped) < 1200


def relpath(path: Path, root: Path) -> str:
    try:
        return path.relative_to(root).as_posix()
    except ValueError:
        return str(path)


def continuation_check_command(root: Path, number: str) -> str:
    chapter_id = f"CH{str(number).zfill(3)}"
    return (
        f'python "{CONTINUATION_CHECK_SCRIPT}" "<kb_context_pack.json>" '
        f'--project-root "{root}" --chapter-id "{chapter_id}" --require-artifacts'
    )


def find_continuation_context(context_dir: Path, number: str) -> Path | None:
    chapter_id = f"ch{str(number).zfill(3)}"
    target_pattern = re.compile(rf"^kb_context_{chapter_id}(?:\b|[_\-.])")
    candidates: list[Path] = []
    if not context_dir.exists() or not context_dir.is_dir():
        return None
    for path in context_dir.glob("*.json"):
        name = path.name.lower()
        if "context" not in name:
            continue
        if not target_pattern.search(name):
            continue
        if "ai_smell" in name or "compare_ai_smell" in name:
            continue
        candidates.append(path)
    if not candidates:
        return None
    return sorted(candidates, key=lambda p: (p.stat().st_mtime, str(p)), reverse=True)[0]


def run_continuation_gate(root: Path, number: str, context_path: Path, timeout_seconds: int = 30) -> dict:
    command = [
        sys.executable,
        str(CONTINUATION_CHECK_SCRIPT),
        str(context_path),
        "--project-root",
        str(root),
        "--chapter-id",
        f"CH{str(number).zfill(3)}",
        "--require-artifacts",
    ]
    result = subprocess.run(
        command,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=timeout_seconds,
    )
    payload: dict = {}
    parse_error = ""
    try:
        payload = json.loads(result.stdout) if result.stdout.strip() else {}
    except json.JSONDecodeError as exc:
        parse_error = str(exc)
    return {
        "chapter": str(number).zfill(3),
        "context": str(context_path),
        "command": " ".join(f'"{item}"' if " " in item else item for item in command),
        "returncode": result.returncode,
        "status": payload.get("status", "error" if result.returncode else "unknown"),
        "failure_signature": payload.get("failure_signature", ""),
        "summary": payload.get("summary", {}),
        "checks": payload.get("checks", []),
        "artifact_paths": payload.get("artifact_paths", {}),
        "stdout_parse_error": parse_error,
        "stderr_tail": result.stderr[-2000:],
    }


def audit_continuation_gate_results(root: Path, chapter_files: list[Path], context_dir: Path | None) -> tuple[list[Finding], list[dict]]:
    if context_dir is None:
        return [], []
    findings: list[Finding] = []
    results: list[dict] = []
    chapter_numbers = sorted(
        {
            number
            for path in chapter_files
            for number in [chapter_number(path)]
            if number and int(number) >= 2
        }
    )
    for number in chapter_numbers:
        context_path = find_continuation_context(context_dir, number)
        if context_path is None:
            findings.append(
                Finding(
                    "P2",
                    "CONTINUATION_GATE_CONTEXT_MISSING",
                    f"CH{number}",
                    "启用了续写状态硬闸自动执行，但未找到本章 NLCL context JSON。",
                    "先用 novel-kb-manager assemble-context 生成本章 context，再重跑 project_audit --run-continuation-gate",
                    {
                        "chapter": number,
                        "context_dir": str(context_dir),
                        "expected_name_hint": f"kb_context_ch{number.lower()}*.json",
                        "continuation_check_command": continuation_check_command(root, number),
                    },
                )
            )
            continue
        try:
            result = run_continuation_gate(root, number, context_path)
        except subprocess.TimeoutExpired:
            result = {
                "chapter": number,
                "context": str(context_path),
                "command": continuation_check_command(root, number),
                "returncode": -1,
                "status": "timeout",
                "failure_signature": "continuation_gate_timeout",
                "summary": {},
                "artifact_paths": {},
                "stderr_tail": "",
            }
        results.append(result)
        if result.get("returncode") == 0 and result.get("status") in {"pass", "skipped"}:
            continue
        findings.append(
            Finding(
                "P1",
                "CONTINUATION_GATE_FAILED",
                f"CH{number}",
                f"续写状态写后硬闸未通过：status={result.get('status')}, failure_signature={result.get('failure_signature') or '<none>'}。",
                "按 continuation gate 输出补控制卡/正文/审稿或换正确 context 后重跑",
                result,
            )
        )
    return findings, results


def _nested_get(payload: dict, path: str, default=None):
    current = payload
    for part in path.split("."):
        if not isinstance(current, dict):
            return default
        current = current.get(part)
    return default if current is None else current


def _ai_smell_report_files(root: Path, extra_dirs: list[Path] | None = None) -> list[Path]:
    scan_roots = [root]
    for extra_dir in extra_dirs or []:
        resolved = extra_dir.expanduser().resolve()
        if resolved not in scan_roots:
            scan_roots.append(resolved)

    files: list[Path] = []
    for scan_root in scan_roots:
        if not scan_root.exists() or not scan_root.is_dir():
            continue
        for pattern in ["*ai_smell_report.json", "*compare_ai_smell_report.json"]:
            files.extend(path for path in scan_root.rglob(pattern) if path.is_file() and not is_excluded_scan_path(path, root))
    return sorted(set(files), key=lambda p: str(p))


def _ai_smell_artifact_role(path: Path, payload: dict) -> str:
    evidence_text = " ".join(
        [
            path.stem,
            str(payload.get("input_path", "")),
        ]
    ).lower()
    if any(marker.lower() in evidence_text for marker in AI_SMELL_FINAL_MARKERS):
        return "final"
    if any(marker.lower() in evidence_text for marker in AI_SMELL_INTERMEDIATE_MARKERS):
        return "intermediate"
    return "unknown"


def audit_ai_smell_reports(root: Path, extra_dirs: list[Path] | None = None) -> tuple[list[Finding], list[dict]]:
    findings: list[Finding] = []
    summaries: list[dict] = []
    for extra_dir in extra_dirs or []:
        resolved = extra_dir.expanduser().resolve()
        if not resolved.exists():
            findings.append(
                Finding(
                    "P2",
                    "AI_SMELL_REPORT_DIR_NOT_FOUND",
                    str(resolved),
                    "指定的 AI smell 报告目录不存在，无法纳入项目审计。",
                    "确认 detect_ai_smell.py 输出目录，或移除 --ai-smell-report-dir 后重跑",
                )
            )
        elif not resolved.is_dir():
            findings.append(
                Finding(
                    "P2",
                    "AI_SMELL_REPORT_DIR_NOT_DIRECTORY",
                    str(resolved),
                    "指定的 AI smell 报告路径不是目录，无法纳入项目审计。",
                    "改传包含 *_ai_smell_report.json 的目录",
                )
            )

    for path in _ai_smell_report_files(root, extra_dirs):
        try:
            payload = json.loads(read_text(path))
        except json.JSONDecodeError as exc:
            findings.append(
                Finding(
                    "P1",
                    "AI_SMELL_REPORT_INVALID_JSON",
                    relpath(path, root),
                    "AI smell 检测报告不是合法 JSON。",
                    "重新运行 detect_ai_smell.py 生成报告",
                    {"error": str(exc)},
                )
            )
            continue

        overall_level = str(_nested_get(payload, "overall_score.level", "")).lower()
        overall_score = _nested_get(payload, "overall_score.score", "")
        blacklist_red = bool(payload.get("blacklist_red_card"))
        drift = payload.get("deai_drift") if isinstance(payload.get("deai_drift"), dict) else {}
        drift_level = str(drift.get("risk_level", "")).lower()
        drift_signature = str(drift.get("failure_signature", "") or "")
        artifact_role = _ai_smell_artifact_role(path, payload)
        blocks_release = artifact_role != "intermediate"
        probe_risks = {
            probe: str(_nested_get(payload, f"{probe}.risk_level", "")).lower()
            for probe in AI_SMELL_RISK_PROBES
        }
        blocking_probes = {
            probe: level
            for probe, level in probe_risks.items()
            if level in AI_SMELL_BLOCKING_LEVELS
        }
        summary = {
            "path": relpath(path, root),
            "source": "project_root" if root in path.parents or path == root else "external_ai_smell_report_dir",
            "input_path": payload.get("input_path", ""),
            "artifact_role": artifact_role,
            "blocks_release": blocks_release,
            "overall_level": overall_level,
            "overall_score": overall_score,
            "blacklist_red_card": blacklist_red,
            "deai_drift_risk_level": drift_level,
            "deai_drift_failure_signature": drift_signature,
            "probe_risks": probe_risks,
        }
        summaries.append(summary)

        if blacklist_red and blocks_release:
            findings.append(
                Finding(
                    "P1",
                    "AI_SMELL_BLACKLIST_RED_CARD",
                    relpath(path, root),
                    "AI smell 检测命中红卡黑名单，不能作为可发布稿。",
                    "回 L5_DEAI_STYLE 或 L4_DRAFT_EXECUTION 重写，并保留人工冷读证据",
                    {
                        **summary,
                        "layer_id": "L5_DEAI_STYLE",
                        "failure_signature": "ai_smell_blacklist_red_card",
                    },
                )
            )
            continue

        if blocks_release and (drift_level in AI_SMELL_BLOCKING_LEVELS or drift_signature):
            findings.append(
                Finding(
                    "P1",
                    "DEAI_DRIFT_RISK",
                    relpath(path, root),
                    f"去 AI/消痕后出现漂移风险：risk_level={drift_level or '<missing>'}, failure_signature={drift_signature or '<none>'}。",
                    "不要用 overall_score 低风险放行；按 deai_drift 定位回 L5_DEAI_STYLE 或 L4_DRAFT_EXECUTION",
                    {
                        **summary,
                        "deai_drift": drift,
                        "layer_id": "L5_DEAI_STYLE",
                        "failure_signature": drift_signature or "deai_drift_risk",
                    },
                )
            )

        if blocks_release and (overall_level in AI_SMELL_BLOCKING_LEVELS or blocking_probes):
            findings.append(
                Finding(
                    "P1",
                    "AI_SMELL_REPORT_RISK",
                    relpath(path, root),
                    f"AI smell 报告存在中高风险：overall={overall_level or '<missing>'}, probes={blocking_probes or '{}'}。",
                    "先做人工冷读复核；若属实，回 L5_DEAI_STYLE 或 L4_DRAFT_EXECUTION，不得仅凭模型自评放行",
                    {
                        **summary,
                        "blocking_probes": blocking_probes,
                        "layer_id": "L5_DEAI_STYLE",
                        "failure_signature": "ai_smell_report_risk",
                    },
                )
            )
    return findings, summaries


def count_manuscript_chars(text: str) -> int:
    return len(re.sub(r"\s+", "", text))


def chapter_number_from_text(text: str) -> str | None:
    for pattern in CHAPTER_TEXT_PATTERNS:
        match = pattern.search(text)
        if not match:
            continue
        value = match.group(match.lastindex or 1)
        return value.zfill(3)
    return None


def _looks_like_final_manuscript(path: Path) -> bool:
    if "05_正文内容" not in path.parts or not chapter_number(path):
        return False
    if "dry_run_handoff" in path.parts:
        return False
    kind = chapter_artifact_kind(path)
    if kind == "final":
        return True
    if kind is not None:
        return False
    if "章纲" in path.parts:
        return False
    return not any(marker in path.name for marker in LONG_FORM_NON_MANUSCRIPT_MARKERS)


def _matched_terms(text: str, terms: list[str]) -> list[str]:
    lower_text = text.lower()
    matched: list[str] = []
    for term in terms:
        if term.lower() in lower_text:
            matched.append(term)
    return matched


def _same_chapter_direct_draft_evidence(
    root: Path,
    markdown_files: list[Path],
    number: str,
) -> list[str]:
    evidence_paths: list[str] = []
    for path in markdown_files:
        if path.name.lower() == "readme.md":
            continue
        if chapter_number(path) != number:
            continue
        if not any(marker in path.name for marker in ["控制卡", "去AI", "去模板化", "审稿", "评分", "报告", "回填", "修改记录"]):
            continue
        text = read_text(path)
        if len(_matched_terms(text, DIRECT_DRAFT_EVIDENCE_TERMS)) >= 2:
            evidence_paths.append(relpath(path, root))
    return evidence_paths


def audit_direct_draft_finalization_gate(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    for path in markdown_files:
        if path.name.lower() == "readme.md":
            continue
        text = read_text(path)
        trigger_terms = _matched_terms(text, DIRECT_DRAFT_TRIGGER_TERMS)
        final_claim_terms = _matched_terms(text, DIRECT_DRAFT_FINAL_CLAIM_TERMS)
        if not trigger_terms or not final_claim_terms:
            continue
        local_evidence_terms = _matched_terms(text, DIRECT_DRAFT_EVIDENCE_TERMS)
        number = chapter_number(path) or chapter_number_from_text(" ".join([path.name, text])) or ""
        same_chapter_evidence = _same_chapter_direct_draft_evidence(root, markdown_files, number) if number else []
        if local_evidence_terms and len(local_evidence_terms) >= 3:
            continue
        if same_chapter_evidence:
            continue

        findings.append(
            Finding(
                "P1",
                "DIRECT_DRAFT_FINALIZATION_UNPROVEN",
                relpath(path, root),
                "文件同时出现 AI 直出/读通顺/risk_draft 信号和定稿/发布/approved 类结论，但缺少控制卡、去 AI、审稿、回填等闭环证据。",
                "补控制卡、去 AI、章节审稿和写后回填证据；证据补齐前只能标 draft_done/risk_draft，不能标 approved/finalized/writeback_done",
                {
                    "chapter": number,
                    "trigger_terms": trigger_terms,
                    "final_claim_terms": final_claim_terms,
                    "local_evidence_terms": local_evidence_terms,
                    "same_chapter_evidence": same_chapter_evidence,
                    "required_evidence": [
                        "control_card_or_chapter_call_list",
                        "deai_done_or_deai_report",
                        "chapter_review_or_human_cold_read",
                        "writeback_or_facts_delta",
                    ],
                    "failure_signature": "direct_draft_only_not_final",
                    "layer_id": "L6_REVIEW_ACCEPTANCE",
                    "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                    "do_not_block": "quick trial drafts and pressure-test risk_draft without final/release claims",
                },
            )
        )
    return findings


def _same_chapter_ai_smell_evidence(number: str, ai_smell_reports: list[dict] | None) -> list[dict]:
    evidence: list[dict] = []
    for item in ai_smell_reports or []:
        if not item.get("blocks_release", True):
            continue
        text = " ".join([str(item.get("path", "")), str(item.get("input_path", ""))])
        if chapter_number_from_text(text) == number:
            evidence.append(item)
    return evidence


def _same_chapter_manual_readability_evidence(
    root: Path,
    markdown_files: list[Path],
    number: str,
) -> list[str]:
    evidence_paths: list[str] = []
    for path in markdown_files:
        if path.name.lower() == "readme.md":
            continue
        if chapter_number(path) != number:
            continue
        if not any(marker in path.name for marker in ["审稿", "评分", "问题卡", "报告", "检测", "冷读"]):
            continue
        text = read_text(path)
        if any(term in text for term in LONG_FORM_READABILITY_EVIDENCE_TERMS):
            evidence_paths.append(relpath(path, root))
    return evidence_paths


def audit_long_form_readability_evidence(
    root: Path,
    markdown_files: list[Path],
    ai_smell_reports: list[dict] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    for path in markdown_files:
        if not _looks_like_final_manuscript(path):
            continue
        text = read_text(path)
        manuscript_chars = count_manuscript_chars(text)
        if manuscript_chars < LONG_FORM_READABILITY_MIN_CHARS:
            continue
        number = chapter_number(path) or ""
        report_evidence = _same_chapter_ai_smell_evidence(number, ai_smell_reports)
        manual_evidence_paths = _same_chapter_manual_readability_evidence(root, markdown_files, number)
        if report_evidence or manual_evidence_paths:
            continue
        findings.append(
            Finding(
                "P2",
                "LONG_FORM_READABILITY_EVIDENCE_MISSING",
                relpath(path, root),
                "2000 字级别正式章节缺少 AI 味/自然顺读验收证据，容易把短碎、生硬直白或语义残缺漏进定稿。",
                "运行 detect_ai_smell.py 生成报告并用 --ai-smell-report-dir 纳入 project_audit；或在同章审稿报告补人工冷读/自然语序/短碎证据",
                {
                    "chapter": number,
                    "char_count": manuscript_chars,
                    "min_chars": LONG_FORM_READABILITY_MIN_CHARS,
                    "accepted_evidence": [
                        "same_chapter_ai_smell_report_blocks_release",
                        "same_chapter_review_with_short_fragment_metrics/natural_order_probe/deai_drift",
                        "same_chapter_manual_cold_read_or_2000字顺读_record",
                    ],
                    "failure_signature": "long_form_readability_evidence_missing",
                    "suggested_command": (
                        f'python "{Path(__file__).resolve().parents[4] / "scripts" / "detect_ai_smell.py"}" '
                        f'"{path}" --output "<AI味报告所在目录>/{path.stem}_ai_smell_report.json"'
                    ),
                },
            )
        )
    return findings


def long_form_ai_smell_gate_candidates(
    root: Path,
    markdown_files: list[Path],
    ai_smell_reports: list[dict] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    for path in markdown_files:
        if not _looks_like_final_manuscript(path):
            continue
        text = read_text(path)
        if count_manuscript_chars(text) < LONG_FORM_READABILITY_MIN_CHARS:
            continue
        number = chapter_number(path) or ""
        if _same_chapter_ai_smell_evidence(number, ai_smell_reports):
            continue
        if _same_chapter_manual_readability_evidence(root, markdown_files, number):
            continue
        candidates.append(path)
    return candidates


def run_ai_smell_gate_for_long_form(
    root: Path,
    markdown_files: list[Path],
    existing_ai_smell_reports: list[dict] | None = None,
    output_dir: Path | None = None,
    timeout_seconds: int = 60,
) -> tuple[list[Finding], list[dict], Path]:
    output_dir = (output_dir or (root / ".project_audit_ai_smell_reports")).expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    candidates = long_form_ai_smell_gate_candidates(root, markdown_files, existing_ai_smell_reports)
    results: list[dict] = []
    findings: list[Finding] = []

    for path in candidates:
        number = chapter_number(path) or "000"
        output_path = output_dir / f"{path.stem}_auto_ai_smell_report.json"
        command = [
            sys.executable,
            str(DETECT_AI_SMELL_SCRIPT),
            str(path),
            "--output",
            str(output_path),
        ]
        result = subprocess.run(
            command,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=timeout_seconds,
        )
        gate_result = {
            "chapter": number,
            "input_path": str(path),
            "output_path": str(output_path),
            "command": " ".join(f'"{item}"' if " " in item else item for item in command),
            "returncode": result.returncode,
            "status": "pass" if result.returncode == 0 and output_path.exists() else "failed",
            "stderr_tail": result.stderr[-2000:],
        }
        results.append(gate_result)
        if gate_result["status"] == "pass":
            continue
        findings.append(
            Finding(
                "P1",
                "AI_SMELL_GATE_FAILED",
                relpath(path, root),
                "自动 AI 味长章顺读门执行失败，不能把长章定稿视为完整闭环。",
                "检查 detect_ai_smell.py、输入章节路径和输出目录后重跑 project_audit --run-ai-smell-gate",
                {
                    **gate_result,
                    "failure_signature": "ai_smell_gate_failed",
                },
            )
        )
    return findings, results, output_dir


def audit_ai_smell_project_bridge(
    root: Path,
    markdown_files: list[Path],
    ai_smell_report_dirs: list[Path] | None = None,
    ai_smell_reports: list[dict] | None = None,
) -> list[Finding]:
    findings: list[Finding] = []
    has_report_dir = bool(ai_smell_report_dirs)
    has_report_summaries = bool(ai_smell_reports)
    for path in markdown_files:
        text = read_text(path)
        if "ai_smell_project_audit_bridge=missing" in text or "ai_smell_project_audit_bridge: missing" in text:
            findings.append(
                Finding(
                    "P1",
                    "AI_SMELL_PROJECT_AUDIT_BRIDGE_MISSING",
                    relpath(path, root),
                    "审稿/压测报告声明 AI 味证据尚未纳入项目体检，不能视为完整闭环。",
                    "用 project_audit.py --ai-smell-report-dir <AI味报告所在目录> 重跑项目体检，并把结果写回审稿/压测报告",
                    {
                        "matched": "ai_smell_project_audit_bridge=missing",
                        "has_ai_smell_report_dir": has_report_dir,
                        "ai_smell_reports_seen": len(ai_smell_reports or []),
                    },
                )
            )
            continue

        if not any(term in text for term in AI_SMELL_BRIDGE_TRIGGER_TERMS):
            continue
        if any(term in text for term in AI_SMELL_BRIDGE_PRESENT_TERMS) or (has_report_dir and has_report_summaries):
            continue

        findings.append(
            Finding(
                "P2",
                "AI_SMELL_PROJECT_AUDIT_BRIDGE_UNPROVEN",
                relpath(path, root),
                "文件引用了 AI 味检测/报告，但未证明项目体检已通过 --ai-smell-report-dir 读取证据目录。",
                "最终项目体检追加 --ai-smell-report-dir，并在审稿/压测报告记录 ai_smell_project_audit_bridge",
                {
                    "trigger_terms": [
                        term for term in AI_SMELL_BRIDGE_TRIGGER_TERMS if term in text
                    ],
                    "has_ai_smell_report_dir": has_report_dir,
                    "ai_smell_reports_seen": len(ai_smell_reports or []),
                },
            )
        )
    return findings


def audit_ai_smell_bridge_payloads(root: Path, markdown_files: list[Path]) -> tuple[list[Finding], list[dict]]:
    findings: list[Finding] = []
    summaries: list[dict] = []
    bridge_payloads: list[tuple[Path, dict]] = []

    for item in markdown_files:
        for payload in _json_blocks(read_text(item)):
            if _is_ai_smell_bridge_payload(payload):
                bridge_payloads.append((item, payload))

    for item in active_project_files(root, "*.json"):
        try:
            payload = json.loads(read_text(item))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _is_ai_smell_bridge_payload(payload):
            bridge_payloads.append((item, payload))

    if not bridge_payloads:
        return findings, summaries

    validator = _load_ai_smell_bridge_validator()
    if validator is None:
        findings.append(
            Finding(
                "P2",
                "AI_SMELL_BRIDGE_VALIDATOR_MISSING",
                str(AI_SMELL_BRIDGE_SCRIPT),
                "发现 ai_smell_project_audit_bridge proof，但校验脚本不可用。",
                "恢复 validate_ai_smell_bridge.py 后重跑项目体检",
                {"validator_path": str(AI_SMELL_BRIDGE_SCRIPT), "payload_count": len(bridge_payloads)},
            )
        )
        return findings, summaries

    for path, payload in bridge_payloads:
        errors = validator.validate_payload(payload)
        summaries.append(
            {
                "path": relpath(path, root),
                "status": payload.get("ai_smell_project_audit_bridge", ""),
                "report_dir": payload.get("report_dir", ""),
                "ai_smell_reports_seen": payload.get("ai_smell_reports_seen", 0),
                "release_blocking_findings": payload.get("release_blocking_findings", 0),
                "project_audit_health": payload.get("project_audit_health", ""),
                "valid": not errors,
            }
        )
        if not errors:
            continue
        error_summary = "; ".join(errors[:2])
        if len(errors) > 2:
            error_summary += f"; ... +{len(errors) - 2} more"
        findings.append(
            Finding(
                "P1",
                "AI_SMELL_BRIDGE_INVALID",
                relpath(path, root),
                f"ai_smell_project_audit_bridge proof 未通过校验（{len(errors)} errors）：{error_summary}",
                "按 ai-smell-bridge-schema.json 修正 proof，或重新运行 project_audit.py --ai-smell-report-dir 后写回",
                {
                    "error_count": len(errors),
                    "errors": errors,
                    "validator": "validate_ai_smell_bridge.py",
                },
            )
        )
    return findings, summaries


def chapter_number(path: Path) -> str | None:
    match = CHAPTER_RE.search(path.stem)
    if not match:
        return None
    return match.group(2).zfill(3)


def chapter_artifact_kind(path: Path) -> str | None:
    stem = path.stem
    for kind, markers in CHAPTER_VERSION_MARKERS.items():
        if any(marker in stem for marker in markers):
            return kind
    return None


def audit_chapter_version_chains(root: Path, markdown_files: list[Path]) -> tuple[list[Finding], list[Path]]:
    chapter_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts and chapter_number(item) and item.name.lower() != "readme.md"
    ]
    chains: dict[str, dict[str, Path]] = {}
    for item in chapter_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        chains.setdefault(number, {})[kind] = item

    findings: list[Finding] = []
    for number, artifacts in sorted(chains.items()):
        has_downstream_work = any(key in artifacts for key in ["draft", "deai", "review", "final", "writeback"])
        if has_downstream_work and "control_card" not in artifacts:
            findings.append(
                Finding(
                    "P1",
                    "CHAPTER_CHAIN_MISSING_CONTROL_CARD",
                    f"05_正文内容/第{number}章",
                    "章节已有正文或审稿产物，但缺少控制卡，无法追溯章节使命。",
                    "先补章节控制卡，再继续正文闭环",
                )
            )
        if "review" in artifacts and "deai" not in artifacts:
            findings.append(
                Finding(
                    "P1",
                    "CHAPTER_CHAIN_MISSING_DEAI",
                    relpath(artifacts["review"], root),
                    "章节已审稿但缺少去模板化稿，版本链未闭合。",
                    "从 L5_DEAI_STYLE 补去模板化稿后重审",
                )
            )
        if "review" in artifacts and "final" not in artifacts:
            findings.append(
                Finding(
                    "P1",
                    "CHAPTER_CHAIN_MISSING_FINAL",
                    relpath(artifacts["review"], root),
                    "章节已审稿但缺少定稿，不能作为 approved 章节使用。",
                    "根据审稿报告修订并生成定稿",
                )
            )
        if "final" in artifacts and "writeback" not in artifacts:
            findings.append(
                Finding(
                    "P1",
                    "CHAPTER_CHAIN_MISSING_WRITEBACK",
                    relpath(artifacts["final"], root),
                    "章节已有定稿但缺少回填记录，动态状态未完成。",
                    "按写后回填协议更新状态、伏笔、总览和修改记录",
                )
            )
    return findings, chapter_files


def audit_writeback_story_tracker_and_timeline(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    formal_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and "dry_run_handoff" not in item.parts
        and chapter_number(item)
    ]
    if not formal_files:
        return findings

    by_chapter: dict[str, dict[str, Path]] = {}
    for item in formal_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        by_chapter.setdefault(number, {})[kind] = item

    for number, artifacts in sorted(by_chapter.items()):
        writeback = artifacts.get("writeback")
        if writeback is None:
            continue

        writeback_text = read_text(writeback)
        missing_story_terms = [
            term for term in STORY_TRACKER_DELTA_REQUIRED_TERMS if term not in writeback_text
        ]
        if missing_story_terms:
            findings.append(
                Finding(
                    "P1",
                    "WRITEBACK_STORY_TRACKER_DELTA_MISSING",
                    relpath(writeback, root),
                    f"CH{number} 已有回填记录，但缺少最小 story_tracker_delta 证据：{', '.join(missing_story_terms)}。",
                    "回 CHxxx_回填记录.md / writeback manifest 补 story_tracker_delta，再进入下一章 assemble-context 或路线验证",
                    {
                        "chapter": number,
                        "missing_terms": missing_story_terms,
                        "required_terms": STORY_TRACKER_DELTA_REQUIRED_TERMS,
                        "failure_signature": "story_tracker_delta_missing",
                        "rewrite_from": "L7_WRITEBACK",
                        "upstream_contract": "writer-entrypoint.md -> Step 5 写回与状态文件",
                    },
                )
            )

        trigger_paths: list[str] = []
        trigger_terms: list[str] = []
        for kind, path in artifacts.items():
            if kind == "writeback":
                continue
            text = read_text(path)
            seen_terms = [term for term in APPEND_ONLY_TIMELINE_TRIGGER_TERMS if term in text]
            if not seen_terms:
                continue
            trigger_paths.append(relpath(path, root))
            for term in seen_terms:
                if term not in trigger_terms:
                    trigger_terms.append(term)

        if not trigger_paths:
            continue

        missing_timeline_terms = [
            term for term in APPEND_ONLY_TIMELINE_REQUIRED_TERMS if term not in writeback_text
        ]
        if not missing_timeline_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "WRITEBACK_APPEND_ONLY_TIMELINE_MISSING",
                relpath(writeback, root),
                f"CH{number} 命中时间线/open debts/系统状态类信号，但回填记录缺少 append_only_timeline 证据：{', '.join(missing_timeline_terms)}。",
                "回 CHxxx_回填记录.md / writeback manifest 追加 append_only_timeline delta，避免静默覆盖旧时间线或未偿债务记录",
                {
                    "chapter": number,
                    "trigger_paths": trigger_paths,
                    "trigger_terms": trigger_terms,
                    "missing_terms": missing_timeline_terms,
                    "required_terms": APPEND_ONLY_TIMELINE_REQUIRED_TERMS,
                    "failure_signature": "append_only_timeline_missing",
                    "rewrite_from": "L7_WRITEBACK",
                    "upstream_contract": "writer-entrypoint.md -> Step 5 写回与状态文件",
                },
            )
        )

    return findings


def audit_dry_run_continuation_handoff(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    dry_run_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and "dry_run_handoff" in item.parts
        and chapter_number(item)
        and int(chapter_number(item) or "0") >= 2
    ]
    if not dry_run_files:
        return findings

    by_chapter: dict[str, dict[str, Path]] = {}
    for item in dry_run_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        by_chapter.setdefault(number, {})[kind] = item

    for number, artifacts in sorted(by_chapter.items()):
        if any(kind in artifacts for kind in HANDOFF_CONTINUATION_REQUIREMENTS):
            control_card = artifacts.get("control_card")
            if control_card is None:
                findings.append(
                    Finding(
                        "P1",
                        "DRY_RUN_CONTINUATION_CONTROL_CARD_MISSING",
                        f"05_正文内容/dry_run_handoff/CH{number}",
                        "CH002+ dry-run handoff 缺少控制卡，无法证明 continuation_state_contract 已生成。",
                        "重跑 dry_run_writer_harness.py，或先补控制卡再进入正文/审稿",
                        {
                            "chapter": number,
                            "required_section": "continuation_state_contract",
                            "continuation_check_command": continuation_check_command(root, number),
                            "continuation_check_note": "Use after dry-run artifacts exist; artifact_missing means the handoff did not generate the expected chapter files.",
                        },
                    )
                )

        for kind, required_terms in HANDOFF_CONTINUATION_REQUIREMENTS.items():
            path = artifacts.get(kind)
            if path is None:
                continue
            text = read_text(path)
            missing_terms = [term for term in required_terms if term not in text]
            if not missing_terms:
                continue
            findings.append(
                Finding(
                    "P1",
                    "DRY_RUN_CONTINUATION_HANDOFF_MISSING",
                    relpath(path, root),
                    f"CH{number} dry-run {kind} 产物缺少 continuation_state 交接证据：{', '.join(missing_terms)}。",
                    "重跑带 continuation_state 的 dry_run_writer_harness.py；若正文已写，按 continuation_state_consumption 重审",
                    {
                        "chapter": number,
                        "artifact_kind": kind,
                        "missing_terms": missing_terms,
                        "failure_signature": "continuation_state_not_consumed",
                        "control_card_layer": "L2_CONTROL_CARD",
                        "draft_layer": "L4_DRAFT_EXECUTION",
                        "continuation_check_command": continuation_check_command(root, number),
                        "continuation_check_note": "Replace <kb_context_pack.json> with the NLCL context used for this chapter; --require-artifacts makes missing or wrong-chapter artifacts fail.",
                    },
                )
            )

    return findings


def audit_formal_continuation_handoff(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    formal_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and "dry_run_handoff" not in item.parts
        and chapter_number(item)
        and int(chapter_number(item) or "0") >= 2
    ]
    if not formal_files:
        return findings

    by_chapter: dict[str, dict[str, Path]] = {}
    for item in formal_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        by_chapter.setdefault(number, {})[kind] = item

    for number, artifacts in sorted(by_chapter.items()):
        trigger_paths: list[str] = []
        for path in artifacts.values():
            text = read_text(path)
            if any(term in text for term in FORMAL_CONTINUATION_TRIGGER_TERMS):
                trigger_paths.append(relpath(path, root))
        if not trigger_paths:
            continue

        control_card = artifacts.get("control_card")
        if control_card is None:
            findings.append(
                Finding(
                    "P1",
                    "FORMAL_CONTINUATION_CONTROL_CARD_MISSING",
                    f"05_正文内容/CH{number}",
                    "CH002+ 正式章节链声明使用 NLCL/continuation_state，但缺少控制卡，无法证明续写状态已转义。",
                    "先补正式控制卡和 continuation_state_contract，再进入正文或审稿",
                    {
                        "chapter": number,
                        "trigger_paths": trigger_paths,
                        "required_section": "continuation_state_contract",
                        "continuation_check_command": continuation_check_command(root, number),
                        "continuation_check_note": "Run after the CHxxx control card, final draft, and review report exist; artifact_missing means the chapter chain is not closed.",
                    },
                )
            )

        for kind, required_terms in FORMAL_CONTINUATION_REQUIREMENTS.items():
            path = artifacts.get(kind)
            if path is None:
                continue
            text = read_text(path)
            missing_terms = [term for term in required_terms if term not in text]
            if not missing_terms:
                continue
            findings.append(
                Finding(
                    "P1",
                    "FORMAL_CONTINUATION_HANDOFF_MISSING",
                    relpath(path, root),
                    f"CH{number} 正式 {kind} 产物声明使用 NLCL/continuation_state，但缺少续写状态消费证据：{', '.join(missing_terms)}。",
                    "补 continuation_state_contract / continuation_state_consumption；若正文已写，按续写状态消费项重审",
                    {
                        "chapter": number,
                        "artifact_kind": kind,
                        "missing_terms": missing_terms,
                        "trigger_paths": trigger_paths,
                        "failure_signature": "continuation_state_not_consumed",
                        "control_card_layer": "L2_CONTROL_CARD",
                        "draft_layer": "L4_DRAFT_EXECUTION",
                        "continuation_check_command": continuation_check_command(root, number),
                        "continuation_check_note": "Replace <kb_context_pack.json> with the NLCL context used for this chapter; artifact_chapter_mismatch means the context and chapter files do not match.",
                    },
                )
            )

    return findings


def audit_execution_pack_node_acceptance(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        if not any(term in text for term in NODE_ACCEPTANCE_TRIGGER_TERMS):
            continue
        missing_terms = [term for term in NODE_ACCEPTANCE_REQUIRED_TERMS if term not in text]
        if not missing_terms:
            continue
        findings.append(
            Finding(
                "P1",
                "EXECUTION_PACK_NODE_ACCEPTANCE_MISSING",
                relpath(path, root),
                f"控制卡声明 execution_pack/轻量执行包，但缺少节点级写前验收门：{', '.join(missing_terms)}。",
                "回 novel-chapter-brief 补 node_acceptance_gate，再进入正文；不要把短句/心理/台词问题留给去 AI 硬盖",
                {
                    "missing_terms": missing_terms,
                    "required_terms": NODE_ACCEPTANCE_REQUIRED_TERMS,
                    "failure_signature": "execution_pack_node_acceptance_missing",
                    "control_card_layer": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                },
            )
        )
    return findings


def system_panel_trigger_terms_seen(text: str) -> list[str]:
    strong_terms = [term for term in SYSTEM_PANEL_STRONG_TRIGGER_TERMS if term in text]
    weak_terms = [
        term
        for term in SYSTEM_PANEL_WEAK_TRIGGER_TERMS
        if term in text and ("系统" in text or "面板" in text or "任务" in text)
    ]
    return strong_terms + [term for term in weak_terms if term not in strong_terms]


def audit_system_panel_effect_contract(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        trigger_terms = system_panel_trigger_terms_seen(text)
        if not trigger_terms:
            continue

        missing_terms = [
            "system_panel_effect_contract"
            if not any(alias in text for alias in SYSTEM_PANEL_CONTRACT_ALIASES)
            else ""
        ]
        missing_terms.extend(term for term in SYSTEM_PANEL_REQUIRED_TERMS if term not in text)
        missing_terms = [term for term in missing_terms if term]
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "SYSTEM_PANEL_EFFECT_CONTRACT_MISSING",
                relpath(path, root),
                f"控制卡命中系统流/系统面板信号，但缺少系统面板现实改价合同字段：{', '.join(missing_terms)}。",
                "回 novel-chapter-brief 补 system_panel_effect_contract，再进正文；不要让面板只做说明书、属性表或奖励到账",
                {
                    "missing_terms": missing_terms,
                    "required_terms": SYSTEM_PANEL_CONTRACT_ALIASES + SYSTEM_PANEL_REQUIRED_TERMS,
                    "trigger_terms": trigger_terms,
                    "failure_signature": "system_panel_without_real_world_effect",
                    "rewrite_from": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "route": "novel-chapter-brief -> novel-volume-folder",
                },
            )
        )
    return findings


def information_reveal_trigger_terms_seen(text: str) -> list[str]:
    return [term for term in INFORMATION_REVEAL_TRIGGER_TERMS if term in text]


def audit_information_reveal_contract(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        trigger_terms = information_reveal_trigger_terms_seen(text)
        if not trigger_terms:
            continue

        missing_terms = [
            "information_reveal_contract"
            if not any(alias in text for alias in INFORMATION_REVEAL_CONTRACT_ALIASES)
            else ""
        ]
        missing_terms.extend(term for term in INFORMATION_REVEAL_REQUIRED_TERMS if term not in text)
        missing_terms = [term for term in missing_terms if term]
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "INFORMATION_REVEAL_CONTRACT_MISSING",
                relpath(path, root),
                f"控制卡命中信息揭露/线索/秘密/设定解释信号，但缺少信息阻力合同字段：{', '.join(missing_terms)}。",
                "回 novel-chapter-brief 补 information_reveal_contract，再进正文；不要让 NPC、旁白或系统把答案免费解释给主角和读者",
                {
                    "missing_terms": missing_terms,
                    "required_terms": INFORMATION_REVEAL_CONTRACT_ALIASES + INFORMATION_REVEAL_REQUIRED_TERMS,
                    "trigger_terms": trigger_terms,
                    "failure_signature": "information_reveal_without_friction",
                    "rewrite_from": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "route": "novel-chapter-brief -> novel-volume-folder",
                },
            )
        )
    return findings


def xianxia_xuanhuan_trigger_terms_seen(text: str) -> list[str]:
    return [term for term in XIANXIA_XUANHUAN_TRIGGER_TERMS if term in text]


def audit_xianxia_xuanhuan_contract(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        trigger_terms = xianxia_xuanhuan_trigger_terms_seen(text)
        if not trigger_terms:
            continue

        missing_terms = [
            "xianxia_xuanhuan_chapter_contract"
            if not any(alias in text for alias in XIANXIA_XUANHUAN_CONTRACT_ALIASES)
            else ""
        ]
        missing_terms.extend(term for term in XIANXIA_XUANHUAN_REQUIRED_TERMS if term not in text)
        missing_terms = [term for term in missing_terms if term]
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "XIANXIA_XUANHUAN_CONTRACT_MISSING",
                relpath(path, root),
                f"控制卡命中修仙/仙侠/玄幻信号，但缺少题材硬骨架合同字段：{', '.join(missing_terms)}。",
                "回 novel-chapter-brief 补 xianxia_xuanhuan_chapter_contract，再进正文；不要让境界、资源、宗门规矩和功法代价只靠模型临场发挥",
                {
                    "missing_terms": missing_terms,
                    "required_terms": XIANXIA_XUANHUAN_CONTRACT_ALIASES + XIANXIA_XUANHUAN_REQUIRED_TERMS,
                    "trigger_terms": trigger_terms,
                    "failure_signature": "xianxia_xuanhuan_contract_missing",
                    "rewrite_from": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "deai_layer_only_if_texture_flattened": "L5_DEAI_STYLE",
                    "route": "novel-chapter-brief -> novel-volume-folder",
                },
            )
        )
    return findings


def prompt_goal_budget_trigger_terms_seen(text: str) -> list[str]:
    return [term for term in PROMPT_GOAL_BUDGET_TRIGGER_TERMS if term in text]


def _find_nested_prompt_goal_budget(payload: object) -> dict | None:
    if isinstance(payload, dict):
        budget = payload.get("prompt_goal_budget")
        if isinstance(budget, dict):
            return budget
        for value in payload.values():
            found = _find_nested_prompt_goal_budget(value)
            if found is not None:
                return found
    elif isinstance(payload, list):
        for value in payload:
            found = _find_nested_prompt_goal_budget(value)
            if found is not None:
                return found
    return None


def _accepted_goals_count_from_json_blocks(text: str) -> int | None:
    for payload in _json_blocks(text):
        budget = _find_nested_prompt_goal_budget(payload)
        if not isinstance(budget, dict):
            continue
        goals = budget.get("accepted_goals")
        if isinstance(goals, list):
            return len(goals)
    return None


def _accepted_goals_count_from_markdown(text: str) -> int | None:
    lines = text.splitlines()
    start: int | None = None
    for index, line in enumerate(lines):
        if "accepted_goals" in line:
            start = index + 1
            suffix = line.split("accepted_goals", 1)[1]
            inline_items = re.findall(r"`[^`]+`|「[^」]+」|\"[^\"]+\"|'[^']+'", suffix)
            if inline_items:
                return len(inline_items)
            break
    if start is None:
        return None

    count = 0
    for line in lines[start:]:
        stripped = line.strip()
        if not stripped:
            continue
        if (
            stripped.startswith("#")
            or re.match(r"^(source_prompt_summary|rejected_or_deferred_rules|conflict_resolution|acceptance_tests)\b", stripped)
            or re.match(r"^[-*]\s*(source_prompt_summary|rejected_or_deferred_rules|conflict_resolution|acceptance_tests)\s*[:：]", stripped)
        ):
            break
        if re.match(r"^[-*]\s+", stripped) or re.match(r"^\d+[.)、]\s+", stripped):
            count += 1
    return count


def accepted_goals_count(text: str) -> int | None:
    count = _accepted_goals_count_from_json_blocks(text)
    if count is not None:
        return count
    return _accepted_goals_count_from_markdown(text)


def prompt_goal_budget_validation_errors(text: str) -> tuple[list[str], bool, str]:
    """Return validator errors, whether a JSON budget was found, and validator status."""
    validator = _load_prompt_goal_budget_validator()
    if validator is None:
        return [f"validator_missing:{PROMPT_GOAL_BUDGET_SCRIPT}"], False, "missing"
    found_json_budget = False
    errors: list[str] = []
    for payload in _json_blocks(text):
        budget = _find_nested_prompt_goal_budget(payload)
        if not isinstance(budget, dict):
            continue
        found_json_budget = True
        errors.extend(validator.validate_payload({"prompt_goal_budget": budget}))
    if found_json_budget:
        return errors, True, "used"
    return [], False, "no_json_budget"


def control_card_contract_validation(path: Path) -> dict:
    validator = _load_control_card_contract_validator()
    if validator is None:
        return {
            "ok": False,
            "status": "error",
            "skipped": False,
            "errors": [f"validator_missing:{CONTROL_CARD_CONTRACT_SCRIPT}"],
            "warnings": [],
            "missing_fields": [],
            "placeholder_fields": [],
            "subterm_gaps": {},
            "required_fields": [],
            "present_fields": [],
            "modes": {},
        }
    args = argparse.Namespace(
        require_nlcl=False,
        require_continuation=False,
        require_xianxia=False,
        json=False,
        paths=[str(path)],
    )
    return validator.validate_file(str(path), args)


def audit_control_card_contracts(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        result = control_card_contract_validation(path)
        if result.get("skipped"):
            continue

        errors = list(result.get("errors", []))
        missing_fields = list(result.get("missing_fields", []))
        placeholder_fields = list(result.get("placeholder_fields", []))
        subterm_gaps = dict(result.get("subterm_gaps", {}))
        if not errors:
            continue

        failure_signature = "control_card_contract_invalid"
        rewrite_from = "L2_CONTROL_CARD"
        if any(field in missing_fields for field in ("continuation_state_contract",)):
            failure_signature = "continuation_state_contract_missing"
        elif any(
            field in missing_fields
            for field in (
                "xianxia_xuanhuan_chapter_contract",
                "realm_state",
                "resource_ledger",
                "sect_or_rule_pressure",
                "technique_cost_limit",
                "visible_trial_or_conflict",
                "earned_payoff",
                "status_delta",
                "future_debt",
            )
        ):
            failure_signature = "xianxia_xuanhuan_contract_missing"
        elif any(
            field in missing_fields
            for field in (
                "context_inclusion_trace",
                "activation_mode_audit",
                "distractor_file_budget",
                "block_context_bundle",
                "readonly_bible_gate",
                "delegated_edit_checkpoint_gate",
                "accepted_version_guard",
            )
        ):
            failure_signature = "context_inclusion_trace_missing"

        findings.append(
            Finding(
                "P1",
                "CONTROL_CARD_CONTRACT_INVALID",
                relpath(path, root),
                f"控制卡 contract 校验未通过：{'; '.join(errors)}。",
                "回 novel-chapter-brief 补齐控制卡成功标准/上下文合同/题材合同后，再进入正文与审稿链",
                {
                    "validator": "validate_control_card_contract.py",
                    "validator_status": result.get("status", ""),
                    "modes": result.get("modes", {}),
                    "required_fields": result.get("required_fields", []),
                    "present_fields": result.get("present_fields", []),
                    "missing_fields": missing_fields,
                    "placeholder_fields": placeholder_fields,
                    "subterm_gaps": subterm_gaps,
                    "warnings": result.get("warnings", []),
                    "errors": errors,
                    "failure_signature": failure_signature,
                    "rewrite_from": rewrite_from,
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "deai_layer_only_if_local_wording": "L5_DEAI_STYLE",
                    "route": "novel-kb-manager audit-spec-success -> novel-chapter-brief -> novel-volume-folder -> novel-chapter-reviewer",
                },
            )
        )
    return findings


def audit_long_form_review_gates(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    formal_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and "dry_run_handoff" not in item.parts
        and chapter_number(item)
    ]
    if not formal_files:
        return findings

    by_chapter: dict[str, dict[str, Path]] = {}
    for item in formal_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        by_chapter.setdefault(number, {})[kind] = item

    for number, artifacts in sorted(by_chapter.items()):
        final_path = artifacts.get("final")
        review_path = artifacts.get("review")
        if final_path is None or review_path is None:
            continue

        final_text = read_text(final_path)
        manuscript_chars = count_manuscript_chars(final_text)
        if manuscript_chars < LONG_FORM_READABILITY_MIN_CHARS:
            continue

        review_text = read_text(review_path)
        missing_terms = [
            term for term in LONG_FORM_REVIEW_GATE_REQUIRED_TERMS if term not in review_text
        ]
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "LONG_FORM_REVIEW_GATE_MISSING",
                relpath(review_path, root),
                f"CH{number} 属于 2000 字级正式定稿，但审稿报告缺少长篇审稿/去AI硬闸字段：{', '.join(missing_terms)}。",
                "回章节审稿补 reader_cost_report、human_friction_surface_pass 和 readability_score_cap；证据补齐前不得把长篇章标为 approved/finalized/writeback_done",
                {
                    "chapter": number,
                    "final_path": relpath(final_path, root),
                    "review_path": relpath(review_path, root),
                    "char_count": manuscript_chars,
                    "min_chars": LONG_FORM_READABILITY_MIN_CHARS,
                    "missing_terms": missing_terms,
                    "required_terms": LONG_FORM_REVIEW_GATE_REQUIRED_TERMS,
                    "failure_signature": "long_form_review_gate_missing",
                    "layer_id": "L6_REVIEW_ACCEPTANCE",
                    "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                    "related_layers_if_root_cause_not_language_only": [
                        "L4_DRAFT_EXECUTION",
                        "L5_DEAI_STYLE",
                    ],
                },
            )
        )
    return findings


def _review_acceptance_json_candidates(root: Path, number: str) -> list[Path]:
    candidates: list[Path] = []
    for path in active_project_files(root, "*.json"):
        name = path.name.lower()
        if "review_acceptance" not in name and "审稿接收" not in path.name:
            continue
        if chapter_number(path) != number and chapter_number_from_text(path.name) != number:
            continue
        candidates.append(path)
    return sorted(candidates, key=lambda item: (item.stat().st_mtime, str(item)), reverse=True)


def _review_acceptance_required_for_chapter(
    final_path: Path | None,
    review_path: Path | None,
) -> tuple[bool, dict]:
    details: dict = {
        "reason": [],
        "final_claim_terms": [],
        "review_claim_terms": [],
        "char_count": 0,
        "min_chars": LONG_FORM_READABILITY_MIN_CHARS,
    }
    if final_path is not None:
        final_text = read_text(final_path)
        manuscript_chars = count_manuscript_chars(final_text)
        details["char_count"] = manuscript_chars
        if manuscript_chars >= LONG_FORM_READABILITY_MIN_CHARS:
            details["reason"].append("long_form_final_over_min_chars")
        final_claim_terms = _matched_terms(final_text, DIRECT_DRAFT_FINAL_CLAIM_TERMS)
        if final_claim_terms:
            details["final_claim_terms"] = final_claim_terms
            details["reason"].append("final_text_claims_approved_or_release_ready")
    if review_path is not None:
        review_text = read_text(review_path)
        review_claim_terms = _matched_terms(review_text, DIRECT_DRAFT_FINAL_CLAIM_TERMS)
        review_claim_terms.extend(_matched_terms(review_text, ["approved", "finalized", "writeback_done", "review_status: approved", "\"review_status\": \"approved\""]))
        review_claim_terms = list(dict.fromkeys(review_claim_terms))
        if review_claim_terms:
            details["review_claim_terms"] = review_claim_terms
            details["reason"].append("review_text_claims_approved_or_writeback_ready")
    return bool(details["reason"]), details


def _review_acceptance_failure_signatures(errors: list[str]) -> list[str]:
    signatures: list[str] = []
    joined_errors = "\n".join(errors).lower()
    if "selection_apply_manifest_gate" in joined_errors:
        if any(token in joined_errors for token in ["diff", "preview", "rollback", "target_span_hash_before", "target_span_ref"]):
            signatures.append("selection_replace_without_diff")
        else:
            signatures.append("selection_apply_manifest_missing")
    if "ai_output_parse_confidence_gate" in joined_errors:
        if any(token in joined_errors for token in ["promote", "truth", "author_decision_ref", "adopted_with_author_decision"]):
            signatures.append("parsed_candidate_promoted_to_truth")
        else:
            signatures.append("ai_output_parse_confidence_unproven")
    if not signatures:
        signatures.append("review_acceptance_artifact_invalid")
    return list(dict.fromkeys(signatures))


def audit_review_acceptance_artifacts(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    formal_files = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and "dry_run_handoff" not in item.parts
        and chapter_number(item)
    ]
    if not formal_files:
        return findings

    by_chapter: dict[str, dict[str, Path]] = {}
    for item in formal_files:
        number = chapter_number(item)
        kind = chapter_artifact_kind(item)
        if not number or not kind:
            continue
        by_chapter.setdefault(number, {})[kind] = item

    validator = _load_review_acceptance_validator()
    if validator is None:
        findings.append(
            Finding(
                "P1",
                "REVIEW_ACCEPTANCE_VALIDATOR_MISSING",
                str(REVIEW_ACCEPTANCE_SCRIPT),
                "正式审稿接收门需要 validate_review_acceptance.py，但脚本不可读取。",
                "恢复 validate_review_acceptance.py 后重跑项目体检",
                {
                    "validator": str(REVIEW_ACCEPTANCE_SCRIPT),
                    "failure_signature": "review_acceptance_validator_missing",
                    "layer_id": "L6_REVIEW_ACCEPTANCE",
                    "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                },
            )
        )
        return findings

    for number, artifacts in sorted(by_chapter.items()):
        final_path = artifacts.get("final")
        review_path = artifacts.get("review")
        required, requirement_details = _review_acceptance_required_for_chapter(final_path, review_path)
        if not required:
            continue

        candidates = _review_acceptance_json_candidates(root, number)
        if not candidates:
            findings.append(
                Finding(
                    "P1",
                    "REVIEW_ACCEPTANCE_GATE_MISSING",
                    f"CH{number}",
                    "章节已进入长章定稿/approved/writeback 类状态，但缺少同章 CHxxx_review_acceptance.json 机器接收产物。",
                    "生成 CHxxx_review_acceptance.json 并通过 validate_review_acceptance.py；证据补齐前不得声称 approved/finalized/writeback_done",
                    {
                        "chapter": number,
                        "final_path": relpath(final_path, root) if final_path else "",
                        "review_path": relpath(review_path, root) if review_path else "",
                        **requirement_details,
                        "expected_artifact": f"CH{number}_review_acceptance.json",
                        "validator": str(REVIEW_ACCEPTANCE_SCRIPT),
                        "failure_signature": "review_acceptance_artifact_missing",
                        "layer_id": "L6_REVIEW_ACCEPTANCE",
                        "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                        "r45_required_gates": [
                            "selection_apply_manifest_gate",
                            "ai_output_parse_confidence_gate",
                        ],
                    },
                )
            )
            continue

        validation_results: list[dict] = []
        valid_paths: list[str] = []
        for candidate in candidates:
            try:
                errors = list(validator.validate_file(str(candidate)))
            except Exception as exc:  # noqa: BLE001 - audit should surface validator crashes.
                errors = [f"validator_exception:{exc.__class__.__name__}: {exc}"]
            result = {
                "path": relpath(candidate, root),
                "ok": not errors,
                "errors": errors,
                "failure_signatures": _review_acceptance_failure_signatures(errors) if errors else [],
            }
            validation_results.append(result)
            if not errors:
                valid_paths.append(relpath(candidate, root))

        if valid_paths:
            continue

        latest = validation_results[0]
        signatures = _review_acceptance_failure_signatures(list(latest.get("errors") or []))
        findings.append(
            Finding(
                "P1",
                "REVIEW_ACCEPTANCE_GATE_INVALID",
                str(latest.get("path") or f"CH{number}"),
                f"同章 review_acceptance.json 存在但未通过机器接收门：{'; '.join((latest.get('errors') or [])[:3])}。",
                "按 review-acceptance-schema.json 与 validate_review_acceptance.py 修正；尤其补齐 selection_apply_manifest_gate 与 ai_output_parse_confidence_gate",
                {
                    "chapter": number,
                    "final_path": relpath(final_path, root) if final_path else "",
                    "review_path": relpath(review_path, root) if review_path else "",
                    **requirement_details,
                    "validator": str(REVIEW_ACCEPTANCE_SCRIPT),
                    "candidate_count": len(candidates),
                    "validation_results": validation_results,
                    "failure_signatures": signatures,
                    "failure_signature": signatures[0],
                    "layer_id": "L6_REVIEW_ACCEPTANCE",
                    "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                    "do_not_claim": ["approved", "finalized", "writeback_done"],
                },
            )
        )
    return findings


def audit_prompt_goal_budget(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        trigger_terms = prompt_goal_budget_trigger_terms_seen(text)
        if not trigger_terms:
            continue

        validator_errors, found_json_budget, validator_status = prompt_goal_budget_validation_errors(text)
        missing_terms = [term for term in PROMPT_GOAL_BUDGET_REQUIRED_TERMS if term not in text]
        goal_count = accepted_goals_count(text)
        goal_count_valid = goal_count is not None and 3 <= goal_count <= 5
        if not goal_count_valid:
            missing_terms.append("accepted_goals_count_3_to_5")
        if not found_json_budget and "prompt_goal_budget" in text:
            missing_terms.append("prompt_goal_budget_json_block")
        if validator_errors:
            missing_terms.append("prompt_goal_budget_schema_or_semantic_error")
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "PROMPT_GOAL_BUDGET_MISSING_OR_INVALID",
                relpath(path, root),
                f"控制卡命中外部提示词/禁用词/润色清单/死命令信号，但缺少有效 prompt_goal_budget：{', '.join(missing_terms)}。",
                "回 novel-chapter-brief 把长提示词和禁用词清单压成 3-5 个 accepted_goals，再进入正文；不要让正文机械执行清单",
                {
                    "missing_terms": missing_terms,
                    "required_terms": PROMPT_GOAL_BUDGET_REQUIRED_TERMS,
                    "accepted_goals_count": goal_count,
                    "accepted_goals_count_required": "3_to_5",
                    "validator": "validate_prompt_goal_budget.py",
                    "validator_status": validator_status,
                    "validator_errors": validator_errors,
                    "trigger_terms": trigger_terms,
                    "failure_signature": "prompt_list_dead_command_overload",
                    "rewrite_from": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "deai_layer_only_if_local_wording": "L5_DEAI_STYLE",
                    "route": "novel-chapter-brief -> novel-volume-folder -> novel-chapter-reviewer",
                },
            )
        )
    return findings


def audit_nlcl_advisory_gate_consumption(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        trigger_terms = [term for term in NLCL_ADVISORY_TRIGGER_TERMS if term in text]
        mentioned_fields = [
            field for field in NLCL_ADVISORY_FIELD_REQUIRED_TERMS
            if field in text
        ]
        if not trigger_terms and not mentioned_fields:
            continue

        missing_by_field: dict[str, list[str]] = {}
        for field in mentioned_fields:
            required_terms = NLCL_ADVISORY_FIELD_REQUIRED_TERMS[field]
            missing_terms = [term for term in required_terms if term not in text]
            if missing_terms:
                missing_by_field[field] = missing_terms

        if not missing_by_field:
            continue

        missing_terms_flat = [
            f"{field}.{term}"
            for field, terms in missing_by_field.items()
            for term in terms
        ]
        findings.append(
            Finding(
                "P1",
                "NLCL_ADVISORY_GATES_UNCONSUMED",
                relpath(path, root),
                f"控制卡接收了 NLCL 成熟度黄灯，但未把黄灯字段转成可验收义务：{', '.join(missing_terms_flat)}。",
                "回 novel-chapter-brief 补 nlcl_advisory_gates 对应字段；压力测试、正式 2000 字章节和低分返工章必须让审稿逐项回应",
                {
                    "trigger_terms": trigger_terms,
                    "mentioned_fields": mentioned_fields,
                    "missing_by_field": missing_by_field,
                    "missing_terms": missing_terms_flat,
                    "required_terms_by_field": NLCL_ADVISORY_FIELD_REQUIRED_TERMS,
                    "failure_signature": "nlcl_advisory_gate_unconsumed",
                    "rewrite_from": "L2_CONTROL_CARD",
                    "draft_layer_if_ignored": "L4_DRAFT_EXECUTION",
                    "deai_layer_if_scope_missing": "L5_DEAI_STYLE",
                    "route": "novel-kb-manager audit-spec-success -> novel-chapter-brief -> novel-chapter-reviewer",
                },
            )
        )
    return findings


def audit_external_reference_closure_visibility(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    control_cards = [
        item
        for item in markdown_files
        if "05_正文内容" in item.parts
        and chapter_artifact_kind(item) == "control_card"
    ]
    for path in control_cards:
        text = read_text(path)
        if not any(term in text for term in EXTERNAL_REFERENCE_CLOSURE_TRIGGER_TERMS):
            continue
        statuses = sorted(
            {
                match.group(1).strip()
                for match in re.finditer(r"source_pool_closure_status\s*:\s*([A-Za-z_]+)", text)
                if match.group(1).strip()
            }
        )
        warning_statuses = [status for status in statuses if status in EXTERNAL_REFERENCE_CLOSURE_WARNING_STATUSES]
        if not warning_statuses:
            continue
        findings.append(
            Finding(
                "P2",
                "EXTERNAL_REFERENCE_CLOSURE_STALE_OR_MISSING",
                relpath(path, root),
                f"控制卡暴露了 external_reference_coverage_closure，但 source_pool_closure_status={', '.join(warning_statuses)}；这只能作为资料池边界提示，不能当成已完全吸收的写作依据。",
                "先回 novel-kb-manager 刷新 audit-external-intake / coverage matrix；写作层继续按 project truth、control card、distillation 和审稿闭环推进。",
                {
                    "statuses": statuses,
                    "warning_statuses": warning_statuses,
                    "trigger_terms": [term for term in EXTERNAL_REFERENCE_CLOSURE_TRIGGER_TERMS if term in text],
                    "advisory_only": True,
                    "failure_signature": "external_reference_closure_visibility_warning",
                    "rewrite_from": "novel-kb-manager audit-external-intake",
                },
            )
        )
    return findings


def _is_revision_signal_file(path: Path) -> bool:
    name = path.name
    markers = [
        "修稿",
        "返工",
        "重写",
        "去AI",
        "去模板化",
        "审稿",
        "评分",
        "问题卡",
        "修改记录",
        "复盘",
        "报告",
    ]
    return any(marker in name for marker in markers)


def audit_revision_plateau_evidence(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    signal_files = [
        item
        for item in markdown_files
        if item.name.lower() != "readme.md" and _is_revision_signal_file(item)
    ]
    for path in signal_files:
        text = read_text(path)
        action_terms = _matched_terms(text, REVISION_PLATEAU_ACTION_TERMS)
        trigger_terms = _matched_terms(text, REVISION_PLATEAU_STRONG_TRIGGER_TERMS)
        if not action_terms or not trigger_terms:
            continue

        missing_terms = [
            term for term in REVISION_PLATEAU_REQUIRED_TERMS if term not in text
        ]
        explicit_plateau_terms = _matched_terms(text, REVISION_PLATEAU_EXPLICIT_TERMS)
        reroute_terms = _matched_terms(text, REVISION_PLATEAU_REROUTE_TERMS)
        if explicit_plateau_terms and not reroute_terms:
            missing_terms.append("revision_plateau_reroute")
        missing_terms = list(dict.fromkeys(missing_terms))
        if not missing_terms:
            continue

        findings.append(
            Finding(
                "P1",
                "REVISION_PLATEAU_EVIDENCE_MISSING",
                relpath(path, root),
                f"修稿/审稿记录命中多轮停滞或平台期信号，但缺少可审计的修稿平台期证据：{', '.join(missing_terms)}。",
                "补 top_issues、before_score、after_score、changed_spans、remaining_blockers；若已平台期，停止刷指标并重路由到 L2/L4/L5/L6 或冷读/人工复核",
                {
                    "missing_terms": missing_terms,
                    "required_terms": REVISION_PLATEAU_REQUIRED_TERMS,
                    "trigger_terms": trigger_terms,
                    "action_terms": action_terms,
                    "explicit_plateau_terms": explicit_plateau_terms,
                    "reroute_terms": reroute_terms,
                    "failure_signature": "revision_plateau_without_evidence",
                    "layer_id": "L6_REVIEW_ACCEPTANCE",
                    "rewrite_from": "L6_REVIEW_ACCEPTANCE",
                    "route": "novel-score-improver -> novel-chapter-reviewer / cold_read",
                    "anti_goodhart_rule": "不要为检测器或单一分数继续无证据循环修稿",
                },
            )
        )
    return findings


def _json_blocks(text: str) -> list[dict]:
    blocks = re.findall(r"```json\s*\n(.*?)\n```", text, re.DOTALL)
    payloads: list[dict] = []
    for raw in blocks:
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            payloads.append(obj)
    return payloads


def _is_packaging_triage_payload(payload: dict) -> bool:
    return (
        payload.get("version") == "v1"
        and "triage_verdict" in payload
        and "return_layer" in payload
        and "next_action" in payload
    )


def _looks_like_issue_card(payload: dict) -> bool:
    issue_markers = {
        "issue_id",
        "priority",
        "problem_symptom",
        "root_cause",
        "failure_signature",
        "rewrite_from",
        "fix_route",
    }
    return "issue_id" in payload or len(issue_markers & set(payload)) >= 3


def _issue_payload_count(payload: dict) -> int:
    if _looks_like_issue_card(payload):
        return 1
    issues = payload.get("issues")
    if isinstance(issues, list):
        return sum(1 for item in issues if isinstance(item, dict) and _looks_like_issue_card(item))
    return 0


def _is_issue_payload(payload: dict) -> bool:
    return _looks_like_issue_card(payload) or _issue_payload_count(payload) > 0


def _is_ai_smell_bridge_payload(payload: dict) -> bool:
    return (
        payload.get("version") == "v1"
        and "ai_smell_project_audit_bridge" in payload
        and "ai_smell_reports_seen" in payload
        and "release_blocking_findings" in payload
    )


def _iter_issue_cards(payload: object) -> list[dict]:
    if isinstance(payload, dict) and _looks_like_issue_card(payload):
        return [payload]
    if isinstance(payload, dict) and isinstance(payload.get("issues"), list):
        return [item for item in payload["issues"] if isinstance(item, dict) and _looks_like_issue_card(item)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict) and _looks_like_issue_card(item)]
    return []


def _latest_status_change(issue: dict) -> dict:
    history = issue.get("status_history")
    if not isinstance(history, list) or not history:
        return {}
    latest = history[-1]
    if not isinstance(latest, dict):
        return {}
    return {
        "latest_status_change_at": latest.get("changed_at", ""),
        "latest_status_change_to": latest.get("to", ""),
        "latest_status_change_actor": latest.get("actor", ""),
        "latest_status_change_note": latest.get("note", ""),
    }


def _status_change_summary(details: dict) -> str:
    latest_to = str(details.get("latest_status_change_to") or "").strip()
    latest_at = str(details.get("latest_status_change_at") or "").strip()
    latest_actor = str(details.get("latest_status_change_actor") or "").strip()
    latest_note = str(details.get("latest_status_change_note") or "").strip()
    if not any([latest_to, latest_at, latest_actor, latest_note]):
        return ""
    pieces = []
    if latest_to:
        pieces.append(f"latest={latest_to}")
    if latest_at:
        pieces.append(latest_at)
    if latest_actor:
        pieces.append(f"by {latest_actor}")
    if latest_note:
        pieces.append(f"note: {latest_note}")
    return "；最近状态变更：" + " / ".join(pieces)


def _markdown_table_cell(value: object) -> str:
    text = str(value)
    text = text.replace("\\", "\\\\").replace("|", "\\|")
    return " ".join(text.splitlines())


def _load_packaging_triage_validator():
    if not PACKAGING_TRIAGE_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_packaging_triage", PACKAGING_TRIAGE_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_unified_issue_validator():
    if not UNIFIED_ISSUE_CARD_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_unified_issue_card", UNIFIED_ISSUE_CARD_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_ai_smell_bridge_validator():
    if not AI_SMELL_BRIDGE_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_ai_smell_bridge", AI_SMELL_BRIDGE_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_prompt_goal_budget_validator():
    if not PROMPT_GOAL_BUDGET_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_prompt_goal_budget", PROMPT_GOAL_BUDGET_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_control_card_contract_validator():
    if not CONTROL_CARD_CONTRACT_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_control_card_contract", CONTROL_CARD_CONTRACT_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _load_review_acceptance_validator():
    if not REVIEW_ACCEPTANCE_SCRIPT.exists():
        return None
    spec = importlib.util.spec_from_file_location("validate_review_acceptance", REVIEW_ACCEPTANCE_SCRIPT)
    if not spec or not spec.loader:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _is_packaging_signal_file(path: Path, root: Path) -> bool:
    rel_parts = set()
    try:
        rel_parts = set(path.relative_to(root).parts)
    except ValueError:
        rel_parts = set(path.parts)
    if rel_parts & PACKAGING_SIGNAL_DIRS:
        return True
    name = path.name
    return any(marker in name for marker in PACKAGING_SIGNAL_NAME_MARKERS)


def _matched_low_read_terms(text: str) -> list[str]:
    return [term for term in LOW_READ_PACKAGING_TERMS if term in text]


def audit_packaging_triage(root: Path, markdown_files: list[Path]) -> list[Finding]:
    findings: list[Finding] = []
    low_read_hits: list[tuple[Path, list[str]]] = []
    triage_payloads: list[tuple[Path, dict]] = []

    signal_markdown_files = [item for item in markdown_files if _is_packaging_signal_file(item, root)]
    for item in signal_markdown_files:
        text = read_text(item)
        matched_terms = _matched_low_read_terms(text)
        if matched_terms:
            low_read_hits.append((item, matched_terms))
        for payload in _json_blocks(text):
            if _is_packaging_triage_payload(payload):
                triage_payloads.append((item, payload))

    for item in active_project_files(root, "*.json"):
        try:
            payload = json.loads(read_text(item))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _is_packaging_triage_payload(payload):
            triage_payloads.append((item, payload))

    if low_read_hits and not triage_payloads:
        first_hit, first_terms = low_read_hits[0]
        hit_paths = [relpath(path, root) for path, _terms in low_read_hits]
        matched_terms = sorted({term for _path, terms in low_read_hits for term in terms})
        findings.append(
            Finding(
                "P1",
                "LOW_READS_WITHOUT_PACKAGING_TRIAGE",
                relpath(first_hit, root),
                "项目记录了低阅读/点击低/没人看等包装触发词，但缺少结构化 packaging_triage。",
                "先补 packaging_triage，再决定修书名、简介、黄金三章还是正文前 500 字",
                {
                    "matched_terms": matched_terms,
                    "first_hit_terms": first_terms,
                    "hit_count": len(low_read_hits),
                    "hit_paths": hit_paths,
                    "required_artifact": "packaging_triage",
                },
            )
        )
        return findings

    validator = _load_packaging_triage_validator()
    if triage_payloads and validator is None:
        findings.append(
            Finding(
                "P2",
                "PACKAGING_TRIAGE_VALIDATOR_MISSING",
                str(PACKAGING_TRIAGE_SCRIPT),
                "发现 packaging_triage，但校验脚本不可用。",
                "恢复 validate_packaging_triage.py 后重跑项目体检",
                {"validator_path": str(PACKAGING_TRIAGE_SCRIPT)},
            )
        )
        return findings

    for path, payload in triage_payloads:
        errors = validator.validate_payload(payload) if validator else []
        if errors:
            error_summary = "; ".join(errors[:2])
            if len(errors) > 2:
                error_summary += f"; ... +{len(errors) - 2} more"
            findings.append(
                Finding(
                    "P1",
                    "PACKAGING_TRIAGE_INVALID",
                    relpath(path, root),
                    f"packaging_triage 未通过校验（{len(errors)} errors）：{error_summary}",
                    "按 packaging-triage-schema.json 修正 triage，再进入低分返工",
                    {
                        "error_count": len(errors),
                        "errors": errors,
                        "triage_verdict": payload.get("triage_verdict", ""),
                        "return_layer": payload.get("return_layer", ""),
                    },
                )
            )

    return findings


def audit_unified_issue_cards(root: Path, markdown_files: list[Path]) -> tuple[list[Finding], int]:
    findings: list[Finding] = []
    issue_payloads: list[tuple[Path, dict, int]] = []

    for item in markdown_files:
        text = read_text(item)
        for payload in _json_blocks(text):
            if _is_issue_payload(payload):
                issue_payloads.append((item, payload, _issue_payload_count(payload)))

    for item in active_project_files(root, "*.json"):
        try:
            payload = json.loads(read_text(item))
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict) and _is_issue_payload(payload):
            issue_payloads.append((item, payload, _issue_payload_count(payload)))
        elif isinstance(payload, list):
            issue_count = sum(1 for row in payload if isinstance(row, dict) and _looks_like_issue_card(row))
            if issue_count:
                issue_payloads.append((item, payload, issue_count))

    total_issue_count = sum(count for _path, _payload, count in issue_payloads)
    if not issue_payloads:
        return findings, 0

    validator = _load_unified_issue_validator()
    if validator is None:
        findings.append(
            Finding(
                "P2",
                "UNIFIED_ISSUE_CARD_VALIDATOR_MISSING",
                str(UNIFIED_ISSUE_CARD_SCRIPT),
                "发现统一问题卡，但校验脚本不可用。",
                "恢复 validate_unified_issue_card.py 后重跑项目体检",
                {"validator_path": str(UNIFIED_ISSUE_CARD_SCRIPT), "payload_count": len(issue_payloads)},
            )
        )
        return findings, total_issue_count

    for path, payload, issue_count in issue_payloads:
        errors = validator.validate_payload(payload) if validator else []
        if errors:
            error_summary = "; ".join(errors[:2])
            if len(errors) > 2:
                error_summary += f"; ... +{len(errors) - 2} more"
            findings.append(
                Finding(
                    "P1",
                    "UNIFIED_ISSUE_CARD_INVALID",
                    relpath(path, root),
                    f"统一问题卡未通过校验（{len(errors)} errors）：{error_summary}",
                    "按 unified-issue-card-schema.json 修正问题卡，再交给 attach_distillation_repair_guidance / novel-score-improver",
                    {
                        "error_count": len(errors),
                        "errors": errors,
                        "issue_count": issue_count,
                        "required_fields": ["issue_id", "priority", "problem_symptom", "layer_id", "rewrite_from"],
                        "validator": "validate_unified_issue_card.py",
                    },
                )
            )

        if errors:
            continue

        open_high_priority = [
            issue
            for issue in _iter_issue_cards(payload)
            if issue.get("priority") in {"P0", "P1"}
            and issue.get("status") not in {"fixed", "rejected"}
        ]
        for issue in open_high_priority[:8]:
            details = {
                "issue_id": issue.get("issue_id", ""),
                "priority": issue.get("priority", ""),
                "status": issue.get("status", ""),
                "problem_symptom": issue.get("problem_symptom", ""),
                "layer_id": issue.get("layer_id", ""),
                "rewrite_from": issue.get("rewrite_from", ""),
                "fix_route": issue.get("fix_route", ""),
                "acceptance_check": issue.get("acceptance_check", ""),
                "update_command": f"python scripts/update_issue_card_status.py \"<issues.json>\" \"{issue.get('issue_id', '')}\" fixed --evidence \"<复审证据>\"",
            }
            details.update(_latest_status_change(issue))
            findings.append(
                Finding(
                    str(issue.get("priority", "P1")),
                    "UNRESOLVED_HIGH_PRIORITY_ISSUE_CARD",
                    relpath(path, root),
                    f"统一问题卡 {issue.get('issue_id', '<missing>')} 仍为 {issue.get('status', '<missing>')}，项目不能视为完整闭环。{_status_change_summary(details)}",
                    "先按 rewrite_from/fix_route 完成返工，复审后把问题卡状态改为 fixed/rejected/deferred",
                    details,
                )
            )

        missing_guidance = [
            issue
            for issue in open_high_priority
            if issue.get("status") in {"open", "needs_user"}
            and issue.get("problem_symptom")
            and "distillation_repair_guidance" not in issue
        ]
        if missing_guidance:
            findings.append(
                Finding(
                    "P2",
                    "ISSUE_CARD_MISSING_DISTILLATION_GUIDANCE",
                    relpath(path, root),
                    f"{len(missing_guidance)} 个未关闭 P0/P1 问题卡缺少 distillation_repair_guidance。",
                    "先运行 scripts/attach_distillation_repair_guidance.py 补返工指导，再交给 novel-score-improver 裁决",
                    {
                        "issue_ids": [str(issue.get("issue_id", "")) for issue in missing_guidance[:12]],
                        "command": 'python scripts/attach_distillation_repair_guidance.py "<issues.json>" --json',
                        "rule": "guidance 只作为辅助证据，不覆盖项目真值、用户冷读和审稿裁决",
                    },
                )
            )

    return findings, total_issue_count


def audit_project(
    root: Path,
    run_continuation_gate: bool = False,
    continuation_context_dir: Path | None = None,
    ai_smell_report_dirs: list[Path] | None = None,
    run_ai_smell_gate: bool = False,
    ai_smell_gate_output_dir: Path | None = None,
) -> dict:
    root = root.expanduser().resolve()
    if continuation_context_dir is not None:
        continuation_context_dir = continuation_context_dir.expanduser().resolve()
    ai_smell_report_dirs = [item.expanduser().resolve() for item in ai_smell_report_dirs or []]
    if ai_smell_gate_output_dir is not None:
        ai_smell_gate_output_dir = ai_smell_gate_output_dir.expanduser().resolve()
    expected_files = load_expected_files()
    findings: list[Finding] = []

    if not root.exists():
        findings.append(
            Finding("P0", "PROJECT_NOT_FOUND", str(root), "项目路径不存在。", "确认路径或先创建项目")
        )
        return build_report(root, findings, [])

    if not root.is_dir():
        findings.append(
            Finding("P0", "PROJECT_NOT_DIRECTORY", str(root), "项目路径不是文件夹。", "改用小说项目根目录")
        )
        return build_report(root, findings, [])

    for dirname in STANDARD_DIRS:
        target = root / dirname
        if not target.exists():
            findings.append(
                Finding("P0", "MISSING_STANDARD_DIR", dirname, "缺少标准 01-09 目录。", "novel-suite-orchestrator")
            )
        elif not target.is_dir():
            findings.append(
                Finding("P0", "STANDARD_DIR_IS_FILE", dirname, "标准目录位置被文件占用。", "人工迁移后重审")
            )

    for rel, _heading, _skill in expected_files:
        target = root / rel
        if not target.exists():
            findings.append(
                Finding("P0", "MISSING_REQUIRED_FILE", rel, "缺少 01-09 标准项目文件。", "对应文件级 skill 补建")
            )
            continue
        if not target.is_file():
            findings.append(
                Finding("P0", "REQUIRED_FILE_IS_DIRECTORY", rel, "标准项目文件位置被目录占用。", "人工整理后重审")
            )
            continue
        text = read_text(target)
        if len(text.strip()) < 20:
            findings.append(
                Finding("P1", "EMPTY_OR_TOO_SHORT", rel, "文件内容为空或过短，无法支撑写作。", "对应文件级 skill 生成内容")
            )
        elif is_placeholder(text):
            findings.append(
                Finding("P1", "PLACEHOLDER_CONTENT", rel, "文件仍像占位内容，尚未形成真值。", "对应文件级 skill 生成内容")
            )

    for rel in KEY_STATE_FILES:
        target = root / rel
        if target.exists() and target.is_file() and is_placeholder(read_text(target)):
            findings.append(
                Finding("P1", "STATE_FILE_NOT_READY", rel, "关键状态/回填文件仍未可用。", "先补状态再写定稿正文")
            )

    for dirname in LEGACY_OR_RISKY_DIRS:
        target = root / dirname
        if target.exists() and dirname not in STANDARD_DIRS:
            findings.append(
                Finding("P1", "LEGACY_OR_PARALLEL_DIR", dirname, "发现旧结构或并行真值目录，可能污染上下文。", "标记归档/镜像/废弃")
            )

    markdown_files = active_project_files(root, "*.md")
    findings.extend(audit_packaging_triage(root, markdown_files))
    issue_card_findings, issue_card_count = audit_unified_issue_cards(root, markdown_files)
    findings.extend(issue_card_findings)
    ai_smell_findings, ai_smell_summaries = audit_ai_smell_reports(root, ai_smell_report_dirs)
    findings.extend(ai_smell_findings)
    ai_smell_gate_results: list[dict] = []
    if run_ai_smell_gate:
        gate_findings, ai_smell_gate_results, gate_output_dir = run_ai_smell_gate_for_long_form(
            root,
            markdown_files,
            ai_smell_summaries,
            ai_smell_gate_output_dir,
        )
        findings.extend(gate_findings)
        gate_report_findings, gate_report_summaries = audit_ai_smell_reports(root, [gate_output_dir])
        findings.extend(gate_report_findings)
        known_report_keys = {
            (item.get("path", ""), item.get("input_path", ""))
            for item in ai_smell_summaries
        }
        for item in gate_report_summaries:
            key = (item.get("path", ""), item.get("input_path", ""))
            if key not in known_report_keys:
                ai_smell_summaries.append(item)
                known_report_keys.add(key)
    ai_smell_bridge_findings, ai_smell_bridge_summaries = audit_ai_smell_bridge_payloads(root, markdown_files)
    findings.extend(ai_smell_bridge_findings)
    findings.extend(
        audit_ai_smell_project_bridge(
            root,
            markdown_files,
            ai_smell_report_dirs,
            ai_smell_summaries,
        )
    )
    chapter_chain_findings, chapter_files = audit_chapter_version_chains(root, markdown_files)
    findings.extend(chapter_chain_findings)
    findings.extend(audit_writeback_story_tracker_and_timeline(root, markdown_files))
    findings.extend(audit_dry_run_continuation_handoff(root, markdown_files))
    findings.extend(audit_formal_continuation_handoff(root, markdown_files))
    findings.extend(audit_execution_pack_node_acceptance(root, markdown_files))
    findings.extend(audit_system_panel_effect_contract(root, markdown_files))
    findings.extend(audit_information_reveal_contract(root, markdown_files))
    findings.extend(audit_xianxia_xuanhuan_contract(root, markdown_files))
    findings.extend(audit_prompt_goal_budget(root, markdown_files))
    findings.extend(audit_control_card_contracts(root, markdown_files))
    findings.extend(audit_nlcl_advisory_gate_consumption(root, markdown_files))
    findings.extend(audit_external_reference_closure_visibility(root, markdown_files))
    findings.extend(audit_revision_plateau_evidence(root, markdown_files))
    findings.extend(audit_long_form_readability_evidence(root, markdown_files, ai_smell_summaries))
    findings.extend(audit_long_form_review_gates(root, markdown_files))
    findings.extend(audit_review_acceptance_artifacts(root, markdown_files))
    findings.extend(audit_direct_draft_finalization_gate(root, markdown_files))
    continuation_gate_results: list[dict] = []
    if run_continuation_gate:
        gate_findings, continuation_gate_results = audit_continuation_gate_results(
            root,
            chapter_files,
            continuation_context_dir or root,
        )
        findings.extend(gate_findings)
    if not chapter_files:
        findings.append(
            Finding("P2", "NO_CHAPTER_FILES", "05_正文内容", "未发现正文章节文件；若还未开写可忽略。", "生成下一章控制卡")
        )

    old_review_like = [
        item for item in markdown_files if any(key in item.name for key in ["审稿", "评分", "问题卡", "修改记录"])
    ]
    if not old_review_like and issue_card_count == 0:
        findings.append(
            Finding("P2", "NO_REVIEW_ARTIFACTS", ".", "未发现审稿/评分/问题卡记录。", "下一次审稿按统一问题卡保存")
        )

    return build_report(
        root,
        findings,
        chapter_files,
        len(expected_files),
        issue_card_count,
        continuation_gate_results,
        ai_smell_summaries,
        ai_smell_bridge_summaries,
        ai_smell_gate_results,
    )


def build_report(
    root: Path,
    findings: list[Finding],
    chapter_files: list[Path],
    expected_file_count: int = 0,
    issue_card_count: int = 0,
    continuation_gate_results: list[dict] | None = None,
    ai_smell_reports: list[dict] | None = None,
    ai_smell_bridge_proofs: list[dict] | None = None,
    ai_smell_gate_results: list[dict] | None = None,
) -> dict:
    severity_rank = {"P0": 0, "P1": 1, "P2": 2}
    counts = {level: sum(1 for finding in findings if finding.severity == level) for level in ["P0", "P1", "P2"]}
    if counts["P0"]:
        health = "blocked"
    elif counts["P1"]:
        health = "degraded"
    else:
        health = "healthy"

    next_actions: list[str] = []
    if counts["P0"]:
        next_actions.append("先补齐缺失目录/标准文件，再进入项目审稿。")
    if any(item.code == "PLACEHOLDER_CONTENT" for item in findings):
        next_actions.append("按 manifest 调用对应文件级 skill，把占位文件生成成可执行真值。")
    if any(item.code == "LEGACY_OR_PARALLEL_DIR" for item in findings):
        next_actions.append("标记旧目录为归档、镜像或废弃，避免旧稿污染上下文。")
    if health != "blocked":
        next_actions.append("运行 novel-project-reviewer 做项目评分，并用统一问题卡交给 novel-score-improver。")
    if chapter_files and health != "blocked":
        next_actions.append("若继续写章，先生成章节控制卡，再走去 AI、章节审稿和回填。")
    elif health != "blocked":
        next_actions.append("若准备开写，先生成第 1 章控制卡并抽样验证可写性。")

    sorted_findings = sorted(findings, key=lambda item: (severity_rank.get(item.severity, 9), item.code, item.path))
    return {
        "project": str(root),
        "health": health,
        "counts": counts,
        "expected_files": expected_file_count,
        "chapter_files": len(chapter_files),
        "issue_cards": issue_card_count,
        "continuation_gate_results": continuation_gate_results or [],
        "ai_smell_reports": ai_smell_reports or [],
        "ai_smell_bridge_proofs": ai_smell_bridge_proofs or [],
        "ai_smell_gate_results": ai_smell_gate_results or [],
        "findings": [asdict(item) for item in sorted_findings],
        "next_actions": next_actions,
    }


def markdown_report(report: dict) -> str:
    lines = [
        "# Novel Project Audit",
        "",
        f"- Project: `{report['project']}`",
        f"- Health: `{report['health']}`",
        f"- Expected files: {report['expected_files']}",
        f"- Chapter files: {report['chapter_files']}",
        f"- Issue cards: {report.get('issue_cards', 0)}",
        f"- Continuation gate results: {len(report.get('continuation_gate_results', []))}",
        f"- AI smell reports: {len(report.get('ai_smell_reports', []))}",
        f"- AI smell gate results: {len(report.get('ai_smell_gate_results', []))}",
        f"- AI smell bridge proofs: {len(report.get('ai_smell_bridge_proofs', []))}",
        f"- Findings: P0={report['counts']['P0']}, P1={report['counts']['P1']}, P2={report['counts']['P2']}",
        "",
        "## Findings",
    ]
    if not report["findings"]:
        lines.append("- No issues found.")
    else:
        lines.append("| severity | code | path | message | route |")
        lines.append("|---|---|---|---|---|")
        for item in report["findings"]:
            lines.append(
                f"| {_markdown_table_cell(item['severity'])} | {_markdown_table_cell(item['code'])} | `{_markdown_table_cell(item['path'])}` | {_markdown_table_cell(item['message'])} | {_markdown_table_cell(item['route'])} |"
            )
        detail_items = [item for item in report["findings"] if item.get("details")]
        if detail_items:
            lines.extend(["", "## Finding Details"])
            for item in detail_items:
                lines.extend(
                    [
                        "",
                        f"### {item['code']} — `{item['path']}`",
                        "",
                        "```json",
                        json.dumps(item["details"], ensure_ascii=False, indent=2),
                        "```",
                    ]
                )
    gate_results = report.get("continuation_gate_results") or []
    if gate_results:
        lines.extend(["", "## Continuation Gate Results"])
        lines.append("| chapter | status | returncode | failure_signature | context |")
        lines.append("|---|---|---|---|---|")
        for item in gate_results:
            lines.append(
                f"| {_markdown_table_cell(item.get('chapter', ''))} | {_markdown_table_cell(item.get('status', ''))} | {_markdown_table_cell(item.get('returncode', ''))} | {_markdown_table_cell(item.get('failure_signature', ''))} | `{_markdown_table_cell(item.get('context', ''))}` |"
            )
    ai_reports = report.get("ai_smell_reports") or []
    if ai_reports:
        lines.extend(["", "## AI Smell Reports"])
        lines.append("| path | overall | score | drift | blacklist |")
        lines.append("|---|---|---|---|---|")
        for item in ai_reports:
            lines.append(
                f"| `{_markdown_table_cell(item.get('path', ''))}` | {_markdown_table_cell(item.get('overall_level', ''))} | {_markdown_table_cell(item.get('overall_score', ''))} | {_markdown_table_cell(item.get('deai_drift_risk_level', ''))} | {_markdown_table_cell(item.get('blacklist_red_card', ''))} |"
            )

    ai_gate_results = report.get("ai_smell_gate_results") or []
    if ai_gate_results:
        lines.extend(["", "## AI Smell Gate Results"])
        lines.append("| chapter | status | returncode | input | output |")
        lines.append("|---|---|---|---|---|")
        for item in ai_gate_results:
            lines.append(
                f"| {_markdown_table_cell(item.get('chapter', ''))} | {_markdown_table_cell(item.get('status', ''))} | {_markdown_table_cell(item.get('returncode', ''))} | `{_markdown_table_cell(item.get('input_path', ''))}` | `{_markdown_table_cell(item.get('output_path', ''))}` |"
            )

    bridge_proofs = report.get("ai_smell_bridge_proofs") or []
    if bridge_proofs:
        lines.extend(["", "## AI Smell Bridge Proofs"])
        lines.append("| path | status | reports_seen | release_blocking | health | valid |")
        lines.append("|---|---|---|---|---|---|")
        for item in bridge_proofs:
            lines.append(
                f"| `{_markdown_table_cell(item.get('path', ''))}` | {_markdown_table_cell(item.get('status', ''))} | {_markdown_table_cell(item.get('ai_smell_reports_seen', ''))} | {_markdown_table_cell(item.get('release_blocking_findings', ''))} | {_markdown_table_cell(item.get('project_audit_health', ''))} | {_markdown_table_cell(item.get('valid', ''))} |"
            )

    lines.extend(["", "## Next Actions"])
    if not report["next_actions"]:
        lines.append("- 项目结构健康，可按当前任务继续。")
    else:
        for action in report["next_actions"]:
            lines.append(f"- {action}")
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("project", help="path to the novel project root")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON instead of Markdown")
    parser.add_argument(
        "--run-continuation-gate",
        action="store_true",
        help="Run check_continuation_state_consumption.py for CH002+ artifacts using context files.",
    )
    parser.add_argument(
        "--continuation-context-dir",
        default="",
        help="Directory containing kb_context_chXXX*.json files; defaults to project root when --run-continuation-gate is used.",
    )
    parser.add_argument(
        "--ai-smell-report-dir",
        action="append",
        default=[],
        help=(
            "Optional directory containing *_ai_smell_report.json or "
            "*_compare_ai_smell_report.json evidence outside the project root. "
            "Can be passed multiple times."
        ),
    )
    parser.add_argument(
        "--run-ai-smell-gate",
        action="store_true",
        help="Run detect_ai_smell.py for long final chapters that lack same-chapter readability evidence.",
    )
    parser.add_argument(
        "--ai-smell-gate-output-dir",
        default="",
        help="Directory for reports generated by --run-ai-smell-gate; defaults to <project>/.project_audit_ai_smell_reports.",
    )
    args = parser.parse_args()

    report = audit_project(
        Path(args.project),
        run_continuation_gate=args.run_continuation_gate,
        continuation_context_dir=Path(args.continuation_context_dir) if args.continuation_context_dir else None,
        ai_smell_report_dirs=[Path(item) for item in args.ai_smell_report_dir],
        run_ai_smell_gate=args.run_ai_smell_gate,
        ai_smell_gate_output_dir=Path(args.ai_smell_gate_output_dir) if args.ai_smell_gate_output_dir else None,
    )
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(markdown_report(report))
    return 1 if report["health"] == "blocked" else 0


if __name__ == "__main__":
    raise SystemExit(main())

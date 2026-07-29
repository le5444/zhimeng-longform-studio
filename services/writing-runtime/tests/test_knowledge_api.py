from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from zhimeng_runtime.app import create_app
from zhimeng_runtime.agent_store import AgentStore
from zhimeng_runtime.config import RuntimeSettings
from zhimeng_runtime.context_models import (
    ContextPack,
    FormalKnowledgeSelector,
    context_pack_hash,
)
from zhimeng_runtime.database import SqliteStore
from zhimeng_runtime.knowledge_service import KnowledgeService, review_issues
from zhimeng_runtime.execution_protocol import (
    CapabilityName,
    CapabilityProfile,
    ExecutorIdentity,
    ExecutorKind,
)
from zhimeng_runtime.project_reader import SECTION_LABELS
from zhimeng_runtime.provenance_models import knowledge_formalization_receipt_id
from zhimeng_runtime.source_import_models import (
    DistillationDraft,
    PreparedSourcePacket,
    SourceArtifact,
    SourceImportJob,
    SourceImportProgress,
    SourceImportStatus,
    TomatoSearchCandidate,
)
from zhimeng_runtime.source_import_receipt_models import (
    SourceImportArtifactEvidence,
    SourceImportExecutionReceipt,
    SourceImportRequestEvidence,
    SourceImportToolEvidence,
    source_import_distillation_input_hash,
    source_import_distillation_output_hash,
    source_import_draft_fingerprint,
    source_import_execution_receipt_hash,
    source_import_idempotency_key,
    source_import_packet_fingerprint,
    source_import_prepare_input_hash,
    source_import_prepare_output_hash,
    source_import_receipt_id,
    source_import_request_hash,
    source_import_run_id,
)


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _artifact(kind: str, path: Path) -> SourceArtifact:
    return SourceArtifact(
        kind=kind,
        path=str(path),
        size_bytes=path.stat().st_size,
        sha256=_sha256(path),
    )


def _reviewed_work_card(
    card_id: str,
    *,
    promise: str = "持续升级的危机、选择、代价与阶段兑现，并在每次回报后留下更高层债务",
) -> dict:
    return {
        "card_id": card_id,
        "card_type": "work",
        "title": "长篇升级循环与读者承诺",
        "source": {
            "name": "测试来源",
            "author": "测试作者",
            "scope": "完整作品与跨章节重复结构复核",
            "evidence_quality": "medium",
            "notes": "依据完整源包、跨章节重复结构和盲样压力测试整理。",
        },
        "taxonomy": {
            "genre": ["玄幻"],
            "scene_function": ["阶段兑现"],
            "emotion": ["压迫", "释放"],
            "craft": ["升级循环"],
        },
        "tags": ["玄幻", "升级", "兑现"],
        "keywords": ["阶段兑现", "升级循环"],
        "applicable_tasks": ["chapter_brief", "draft_scene", "review"],
        "summary": "通过可见压力、有限选择、付出代价和阶段兑现组成可重复但可变形的长篇推进循环，并让每一次回报真实改变人物处境与下一步行动空间。",
        "reader_promise": promise,
        "reader_effect": {
            "author_intent": "让读者明确感到每次选择都在推进力量与关系。",
            "observed_reader_effect": "读者在小兑现后仍会追问下一次更高代价的选择。",
            "effect_evidence": ["跨章节样本中，小兑现后均留下了更高层级的新债务。"],
            "effect_failure_boundary": "连续两章只有升级说明而没有选择和代价时会变成流水账。",
            "misread_risk": "只复制升级名词会丢失压力、选择与代价之间的因果。",
            "when_it_turns_bad": "回报不改变人物处境，或新债务与旧选择没有关系时。",
        },
        "type_payload": {
            "premise_pattern": "主角在资源受限时以承担代价换取新的行动空间。",
            "core_reader_promise": promise,
            "opening_contract": "开场先展示可见损失，再给出只能冒险验证的有限机会。",
            "progression_map": ["压力外显后做选择", "付出代价后小兑现", "兑现带出更大债务"],
            "recurring_scene_loops": ["资源受限 -> 风险试探 -> 小兑现 -> 更高压力"],
            "transfer_limits": ["不得复制来源人物", "不得复制来源设定壳", "不得复刻场景顺序"],
        },
        "mechanics": [
            {
                "name": "代价绑定的阶段兑现",
                "use_when": "章节需要同时推进力量、关系与下一阶段压力时。",
                "rule": "先让角色做不可撤销的选择，再用小兑现证明选择有效并抬高后续代价。",
                "failure_mode": "先给奖励再补代价，会让选择失去重量并削弱追读。",
            }
        ],
        "validation_tests": [
            "本章兑现是否改变角色下一步可做的选择，而不只是增加数值。",
            "新增债务是否能追溯到本章选择和代价，而不是凭空追加危机。",
        ],
        "validation_ladder": {
            step: {
                "status": "pass",
                "evidence": [f"{step} 已在独立样本中完成可复核验证。"],
            }
            for step in (
                "internal_consistency",
                "predictive_validation",
                "blind_sample_validation",
                "extreme_scenario_stress_test",
            )
        },
        "scores": {
            "evidence": 0.72,
            "specificity": 0.76,
            "generativity": 0.78,
            "retrieval": 0.68,
            "safety_risk": 0.18,
        },
        "retrieval": {
            "preferred_queries": ["玄幻 升级 阶段兑现"],
            "negative_queries": ["纯日常 无升级"],
        },
        "nlcl_route_hints": {
            "recommended_profiles": ["chapter", "draft_assist", "review"],
            "nlcl_layers": ["distillation_index"],
            "route_terms": ["玄幻", "升级", "阶段兑现"],
            "hard_filters": {"genre": ["玄幻"]},
            "negative_routes": ["纯日常"],
            "safety_summary": "只使用抽象推进机制，不使用来源人物和设定壳。",
            "feedback_default_layer": "L3_DISTILLATION_RETRIEVAL",
        },
        "safety": {
            "copyright_boundary": "只保留可迁移机制，不保存来源表达和可识别场景壳。",
            "do_not_reuse": ["来源人物", "来源设定壳", "来源场景顺序"],
            "source_entity_names": [],
            "source_setting_shells": [],
            "signature_phrases": [],
            "contamination_risks": ["source_shell", "entity_leakage"],
        },
        "draft_candidate": False,
        "requires_human_distillation": False,
    }


def _reviewed_voice_card(card_id: str) -> dict:
    card = deepcopy(_reviewed_work_card(card_id))
    card.update(
        {
            "card_type": "voice",
            "title": "贴近人物压力的长篇叙事声音规则",
            "summary": (
                "保持贴近人物判断的有限视角，以短中长句交替承接动作、关系和代价，"
                "并用可观察的现场变化替代抽象总结，避免复制来源作者的可识别声纹。"
            ),
            "reader_promise": (
                "读者能顺畅读懂人物正在做什么、为什么犹豫以及选择会付出什么代价。"
            ),
            "taxonomy": {
                "platform": ["番茄"],
                "audience": ["男频"],
                "genre": ["玄幻"],
                "style": ["现场压力", "自然网文"],
                "pov": ["贴近主角的有限视角"],
                "content_boundary": ["只迁移抽象叙事行为"],
            },
            "tags": ["番茄", "男频", "玄幻", "有限视角", "自然网文", "现场压力"],
            "keywords": ["人物压力", "自然叙事"],
            "applicable_tasks": ["draft_scene", "rewrite", "review"],
            "type_payload": {
                "pov_distance": "贴近当前行动人物的判断，但不直接替角色总结全部情绪。",
                "sentence_rhythm": "动作处用短句落点，关系变化用中句承接，信息解释不连续堆叠。",
                "diction_pool": ["具体动作与物件", "人物关系站位", "可见资源和代价"],
                "dialogue_behavior": "每句对话携带目的、关系站位或信息差，不让角色轮流讲设定。",
                "description_behavior": "描写必须改变人物判断或行动空间，不铺陈与场景无关的气氛。",
                "emotion_handling": "先写身体反应、动作选择和关系后果，再让读者自己得出情绪。",
                "human_web_prose": "句子语义完整、主谓清楚、落在当前场景，连续阅读不出现报告腔。",
                "sentence_variety": "短中长句按压力变化交替，禁止连续碎句和为了酷感破坏自然语序。",
                "scene_anchor_rules": ["每两到三段出现一次会改变判断的动作、物件或资源变化。"],
                "dialogue_pressure_rules": ["每句对话至少推进目的、关系站位或信息差中的一项。"],
                "anti_ai_failure_signatures": [
                    "连续机械碎句",
                    "抽象心理替代动作",
                    "角色同声解释设定",
                ],
                "readability_acceptance_tests": [
                    "连续阅读两千字时能分清谁在行动、谁在害怕以及谁正在承担代价。"
                ],
                "do_not_copy": [
                    "来源作者标志句",
                    "来源作品节奏链",
                    "来源人物口头禅",
                ],
            },
            "nlcl_route_hints": {
                "recommended_profiles": ["chapter", "draft_assist", "review"],
                "nlcl_layers": ["distillation_index", "style", "deai"],
                "route_terms": ["玄幻", "人物压力", "自然叙事"],
                "hard_filters": {"genre": ["玄幻"]},
                "negative_routes": ["作者声纹模仿", "来源句式复刻"],
                "safety_summary": "只迁移抽象叙事行为，不复制来源作者的声纹和表达。",
                "feedback_default_layer": "L5_DEAI_STYLE",
            },
        }
    )
    return card


def _reviewed_validator_card(card_id: str) -> dict:
    card = deepcopy(_reviewed_work_card(card_id))
    card.update(
        {
            "card_type": "validator",
            "title": "人物压力与来源污染的正文拒收闸门",
            "summary": (
                "在正文候选进入作者审阅前检查人物压力是否落到动作和代价，并阻断来源名称、"
                "设定壳、标志句或可识别场景顺序进入当前项目。"
            ),
            "reader_promise": (
                "只有动作、关系和代价可追溯且没有来源污染的正文候选才能进入作者审阅。"
            ),
            "taxonomy": {
                "platform": ["番茄"],
                "audience": ["男频"],
                "genre": ["玄幻"],
                "problem_symptom": ["抽象总结", "来源污染"],
                "content_boundary": ["正文候选拒收闸门"],
                "craft": ["人物压力"],
            },
            "tags": ["番茄", "男频", "玄幻", "正文审阅", "来源污染", "拒收闸门"],
            "keywords": ["人物压力", "来源污染"],
            "applicable_tasks": ["review", "rewrite"],
            "type_payload": {
                "validator_name": "character_pressure_and_source_contamination_gate",
                "applies_to": ["完整章节正文候选", "针对性正文改写候选"],
                "layer_id": "L6_REVIEW_ACCEPTANCE",
                "checks": [
                    "人物选择是否通过动作、关系或资源变化产生可见代价。",
                    "正文是否避开来源名称、设定壳、标志句和可识别场景顺序。",
                ],
                "reject_conditions": [
                    "连续使用抽象心理总结替代人物行动和关系后果。",
                    "出现来源专名、标志表达或未经迁移的完整场景顺序。",
                ],
                "execution_rules": [
                    {
                        "rule_id": "forbid_source_terms",
                        "kind": "forbid_terms",
                        "terms": ["Archive Night", "source shell"],
                        "case_sensitive": False,
                        "failure_signature": "validator_source_term_leak",
                        "message": "正文候选包含正式 validator 明确禁止的来源词项。",
                    },
                    {
                        "rule_id": "require_visible_pressure",
                        "kind": "require_any_term",
                        "terms": ["before", "代价", "压力", "选择"],
                        "case_sensitive": False,
                        "failure_signature": "validator_visible_pressure_missing",
                        "message": "正文候选缺少正式 validator 要求的可见压力或代价词项。",
                    },
                ],
                "rewrite_routes": ["L4_DRAFT_EXECUTION", "L5_DEAI_STYLE"],
            },
            "nlcl_route_hints": {
                "recommended_profiles": ["chapter", "review", "rewrite"],
                "nlcl_layers": ["distillation_index", "review"],
                "route_terms": ["玄幻", "人物压力", "来源污染"],
                "hard_filters": {"genre": ["玄幻"]},
                "negative_routes": ["只做文案建议", "跳过拒收条件"],
                "safety_summary": "发现来源污染或人物压力失真时必须拒收并返回指定层重写。",
                "feedback_default_layer": "L6_REVIEW_ACCEPTANCE",
            },
        }
    )
    return card


def test_work_voice_and_validator_use_distinct_formal_review_contracts() -> None:
    work = _reviewed_work_card("work-contract-test")
    voice = _reviewed_voice_card("voice-contract-test")
    validator = _reviewed_validator_card("validator-contract-test")

    assert review_issues(work) == []
    assert review_issues(voice) == []
    assert review_issues(validator) == []

    invalid_voice = deepcopy(voice)
    invalid_voice["type_payload"]["do_not_copy"] = ["太短"]
    invalid_voice["nlcl_route_hints"]["feedback_default_layer"] = (
        "L3_DISTILLATION_RETRIEVAL"
    )
    voice_issues = review_issues(invalid_voice)
    assert {
        (item.code, item.field)
        for item in voice_issues
    } >= {
        ("field_too_shallow", "type_payload.do_not_copy"),
        ("invalid_feedback_layer", "nlcl_route_hints.feedback_default_layer"),
    }

    invalid_validator = deepcopy(validator)
    invalid_validator["type_payload"]["layer_id"] = "UNKNOWN_LAYER"
    invalid_validator["type_payload"]["reject_conditions"] = []
    validator_issues = review_issues(invalid_validator)
    assert {
        (item.code, item.field)
        for item in validator_issues
    } >= {
        ("invalid_validator_layer", "type_payload.layer_id"),
        ("field_too_shallow", "type_payload.reject_conditions"),
        ("invalid_feedback_layer", "nlcl_route_hints.feedback_default_layer"),
    }

    duplicate_signature = deepcopy(validator)
    rules = duplicate_signature["type_payload"]["execution_rules"]
    rules[1]["failure_signature"] = rules[0]["failure_signature"]
    assert (
        "duplicate_validator_failure_signature",
        "type_payload.execution_rules",
    ) in {
        (item.code, item.field)
        for item in review_issues(duplicate_signature)
    }


def _draft_cards(card_id: str) -> list[dict]:
    work = _reviewed_work_card(card_id)
    work["draft_candidate"] = True
    work["requires_human_distillation"] = True
    work["source"]["evidence_quality"] = "weak"
    work["summary"] = "Source packet draft work card pending review."
    work["type_payload"]["premise_pattern"] = "pending_human_distillation"
    for entry in work["validation_ladder"].values():
        entry["status"] = "warn"
        entry["evidence"] = ["not yet validated"]
    work["scores"] = {
        "evidence": 0.3,
        "specificity": 0.35,
        "generativity": 0.3,
        "retrieval": 0.25,
        "safety_risk": 0.2,
    }
    return [
        work,
        {
            "card_id": f"voice-{card_id}",
            "card_type": "voice",
            "draft_candidate": True,
            "requires_human_distillation": True,
        },
        {
            "card_id": f"validator-{card_id}",
            "card_type": "validator",
            "draft_candidate": True,
            "requires_human_distillation": True,
        },
    ]


def _seed_source_import(
    store: SqliteStore,
    database_path: Path,
    *,
    import_id: str,
    card_id: str,
    cards_payload: list[object] | None = None,
) -> SourceImportExecutionReceipt:
    root = database_path.parent / "source-imports" / import_id
    packet = root / "packet"
    output = root / "distillation-draft"
    packet.mkdir(parents=True)
    output.mkdir()
    packet_files: dict[str, Path] = {}
    for kind, name, content in (
        ("packet_manifest", "packet.manifest.json", "{}"),
        ("packet_epub", "source.epub", "epub"),
        ("downloaded_epub", "downloaded.epub", "epub"),
        ("packet_text", "source.txt", "正文"),
        ("packet_metadata", "metadata.json", "{}"),
    ):
        path = packet / name
        path.write_text(content, encoding="utf-8")
        packet_files[kind] = path
    cards = output / "cards.draft.json"
    cards.write_text(
        __import__("json").dumps(
            cards_payload if cards_payload is not None else _draft_cards(card_id),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    draft_files: dict[str, Path] = {"cards_draft_json": cards}
    for kind, name in (
        ("distillation_report_md", "distillation_report.md"),
        ("validation_report_md", "validation_report.md"),
        ("tool_result_envelope_json", "tool_result_envelope.json"),
    ):
        path = output / name
        path.write_text(
            (
                '{"schema":"novel_distillation.source_packet_draft.v1",'
                '"status":"draft_ready"}'
                if name.endswith(".json")
                else "# receipt\n"
            ),
            encoding="utf-8",
        )
        draft_files[kind] = path
    source_packet = PreparedSourcePacket(
        title="测试作品",
        author="测试作者",
        download_scope="full",
        packet_dir=str(packet),
        packet_manifest_path=str(packet_files["packet_manifest"]),
        epub_path=str(packet_files["packet_epub"]),
        downloaded_epub_path=str(packet_files["downloaded_epub"]),
        text_path=str(packet_files["packet_text"]),
        metadata_path=str(packet_files["packet_metadata"]),
        char_count=100,
        chapter_marker_count=1,
        artifacts=[_artifact(kind, path) for kind, path in packet_files.items()],
    )
    draft = DistillationDraft(
        output_dir=str(output),
        cards_path=str(cards),
        report_path=str(draft_files["distillation_report_md"]),
        validation_report_path=str(draft_files["validation_report_md"]),
        envelope_path=str(draft_files["tool_result_envelope_json"]),
        card_count=len(cards_payload) if cards_payload is not None else 3,
        artifacts=[_artifact(kind, path) for kind, path in draft_files.items()],
    )
    now = datetime.now(UTC)
    job = SourceImportJob(
        import_id=import_id,
        query="测试作品",
        candidate=TomatoSearchCandidate(
            index=1,
            book_id="12345",
            title="测试作品",
            author="测试作者",
        ),
        status=SourceImportStatus.AWAITING_REVIEW,
        progress=SourceImportProgress(
            stage="awaiting_review",
            message="Draft cards are ready for review",
            percent=100,
        ),
        source_packet=source_packet,
        distillation_draft=draft,
        created_at=now,
        updated_at=now,
    )
    initial = job.model_copy(
        update={
            "status": SourceImportStatus.DRAFTING,
            "distillation_draft": None,
            "progress": SourceImportProgress(
                stage="drafting",
                message="Building a fixture draft",
                percent=100,
            ),
        }
    )
    store.create_source_import_job(initial.model_dump(mode="json", by_alias=True))

    state_root = database_path.parent / "source-imports"
    source_evidence = tuple(
        sorted(
            (
                SourceImportArtifactEvidence(
                    kind=kind,
                    relative_path=path.relative_to(state_root).as_posix(),
                    size_bytes=path.stat().st_size,
                    sha256=_sha256(path),
                )
                for kind, path in packet_files.items()
            ),
            key=lambda item: (item.kind, item.relative_path),
        )
    )
    draft_evidence = tuple(
        sorted(
            (
                SourceImportArtifactEvidence(
                    kind=kind,
                    relative_path=path.relative_to(state_root).as_posix(),
                    size_bytes=path.stat().st_size,
                    sha256=_sha256(path),
                )
                for kind, path in draft_files.items()
            ),
            key=lambda item: (item.kind, item.relative_path),
        )
    )
    request = SourceImportRequestEvidence(
        query=job.query,
        candidate_index=job.candidate.index,
        book_id=job.candidate.book_id,
        title=job.candidate.title,
        author=job.candidate.author,
    )
    request_hash = source_import_request_hash(request)
    prepare_tool = SourceImportToolEvidence(
        tool_id="fixture.tomato-prepare",
        tool_version="1",
        tool_fingerprint="1" * 64,
    )
    distillation_tool = SourceImportToolEvidence(
        tool_id="fixture.distillation",
        tool_version="1",
        tool_fingerprint="2" * 64,
    )
    packet_fingerprint = source_import_packet_fingerprint(
        request_hash,
        "full",
        source_evidence,
    )
    manifest_hash = _sha256(packet_files["packet_manifest"])
    text_hash = _sha256(packet_files["packet_text"])
    metadata_hash = _sha256(packet_files["packet_metadata"])
    distillation_input_hash = source_import_distillation_input_hash(
        packet_fingerprint,
        manifest_hash,
        text_hash,
        metadata_hash,
    )
    draft_fingerprint = source_import_draft_fingerprint(
        distillation_input_hash,
        distillation_tool.tool_fingerprint,
        draft_evidence,
    )
    cards_hash = _sha256(cards)
    envelope_hash = _sha256(draft_files["tool_result_envelope_json"])
    receipt_material = {
        "receipt_id": source_import_receipt_id(import_id),
        "source_import_id": import_id,
        "run_id": source_import_run_id(import_id),
        "revision": 1,
        "idempotency_key": source_import_idempotency_key(
            source_import_run_id(import_id),
            1,
            request_hash,
        ),
        "request": request,
        "request_hash": request_hash,
        "retry_of": None,
        "prepare_invocation_id": "source-import-invocation-"
        + hashlib.sha256(f"{import_id}:prepare".encode()).hexdigest()[:32],
        "prepare_tool": prepare_tool,
        "prepare_input_hash": source_import_prepare_input_hash("tomato", request_hash),
        "prepare_output_hash": source_import_prepare_output_hash(
            packet_fingerprint,
            manifest_hash,
            text_hash,
            metadata_hash,
        ),
        "source_packet_fingerprint": packet_fingerprint,
        "packet_manifest_hash": manifest_hash,
        "packet_text_hash": text_hash,
        "packet_metadata_hash": metadata_hash,
        "source_artifacts": source_evidence,
        "distillation_invocation_id": "source-import-invocation-"
        + hashlib.sha256(f"{import_id}:distill".encode()).hexdigest()[:32],
        "distillation_tool": distillation_tool,
        "distillation_input_hash": distillation_input_hash,
        "distillation_output_hash": source_import_distillation_output_hash(
            draft_fingerprint,
            cards_hash,
            envelope_hash,
            draft.card_count,
        ),
        "draft_fingerprint": draft_fingerprint,
        "cards_draft_hash": cards_hash,
        "envelope_hash": envelope_hash,
        "envelope_schema": "novel_distillation.source_packet_draft.v1",
        "envelope_status": "draft_ready",
        "card_count": draft.card_count,
        "draft_artifacts": draft_evidence,
        "created_at": now,
    }
    provisional = SourceImportExecutionReceipt.model_construct(
        **receipt_material,
        receipt_hash="0" * 64,
    )
    receipt = SourceImportExecutionReceipt(
        **receipt_material,
        receipt_hash=source_import_execution_receipt_hash(provisional),
    )
    store.save_source_import_job_with_execution_receipt(
        job.model_dump(mode="json", by_alias=True),
        receipt.model_dump(mode="json", by_alias=True),
    )
    return receipt


def _write_project(root: Path) -> Path:
    root.mkdir(parents=True)
    for index, label in SECTION_LABELS.items():
        (root / f"{index:02d}_{label}").mkdir()
    (root / "01_项目定位" / "定位.md").write_text("玄幻长篇升级故事。", encoding="utf-8")
    (root / "02_世界观设定" / "世界.md").write_text("力量必须以代价换取。", encoding="utf-8")
    (root / "03_人物设定" / "主角.md").write_text("主角拒绝无代价的力量。", encoding="utf-8")
    (root / "04_剧情大纲" / "主线.md").write_text("每次兑现都抬高下一次选择成本。", encoding="utf-8")
    volume = root / "05_正文内容" / "第一卷"
    volume.mkdir()
    (volume / "CH001_正文.md").write_text("第一章，主角用代价换来一次小兑现。", encoding="utf-8")
    (root / "06_写作规范" / "风格.md").write_text("事实先于抒情。", encoding="utf-8")
    (root / "07_运营数据" / "状态.md").write_text("连载中。", encoding="utf-8")
    return root


def _settings(tmp_path: Path, *, trusted_project_roots: tuple[Path, ...] = ()) -> RuntimeSettings:
    return RuntimeSettings(
        environment="test",
        database_path=tmp_path / "runtime.sqlite3",
        allowed_origins=(),
        trusted_project_roots=trusted_project_roots,
    )


def _publish_project_kb(
    client: TestClient,
    project: dict,
) -> dict[str, str]:
    project_id = str(project["project_id"])
    knowledge_response = client.get(
        f"/api/v1/projects/{project_id}/knowledge/cards"
    )
    assert knowledge_response.status_code == 200, knowledge_response.text
    knowledge = knowledge_response.json()
    rebuilt = client.post(
        f"/api/v1/projects/{project_id}/knowledge/kb-state/rebuild",
        json={
            "expected_project_revision": project["revision"],
            "expected_project_fingerprint": project["fingerprint"],
            "expected_scope_state_revision": knowledge["scope"]["state_revision"],
            "expected_knowledge_fingerprint": knowledge["fingerprint"],
        },
    )
    assert rebuilt.status_code == 200, rebuilt.text
    record = rebuilt.json()["record"]
    assert record["status"] == "current"
    manifest = record["manifest"]
    return {
        "project_kb_state_id": manifest["state_id"],
        "project_kb_manifest_hash": manifest["manifest_hash"],
    }


def _seed_runtime_deep_invocation(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
    *,
    receipt: SourceImportExecutionReceipt,
    project: dict,
    knowledge_path: str,
) -> dict:
    """Persist a real Runtime-deep invocation ledger and its three candidates."""

    fixtures = __import__("test_distillation_invocation_models")
    from zhimeng_runtime.distillation_invocation_models import (
        DISTILLATION_INSTRUCTION_SKILL_ID,
        DistillationInstructionBinding,
        DistillationKnowledgeBinding,
        DistillationProjectBinding,
        DistillationProviderBinding,
        DistillationSourceBinding,
    )
    from zhimeng_runtime.distillation_invocation_store import (
        DistillationInvocationStore,
    )

    scope_response = client.get(f"{knowledge_path}/scope")
    assert scope_response.status_code == 200, scope_response.text
    knowledge_response = client.get(f"{knowledge_path}/cards")
    assert knowledge_response.status_code == 200, knowledge_response.text
    scope = scope_response.json()
    knowledge = knowledge_response.json()
    idempotency_key = (
        "distillation-idempotency-"
        + hashlib.sha256(receipt.source_import_id.encode()).hexdigest()[:32]
    )
    instruction = DistillationInstructionBinding(
        skill_id=DISTILLATION_INSTRUCTION_SKILL_ID,
        skill_version_id="skill-version-" + hashlib.sha256(
            f"{receipt.source_import_id}:skill-version".encode()
        ).hexdigest()[:32],
        skill_version="1.0.0",
        manifest_fingerprint="a" * 64,
        package_fingerprint="b" * 64,
        activation_receipt_id="skill-activation-" + hashlib.sha256(
            f"{receipt.source_import_id}:activation".encode()
        ).hexdigest()[:32],
        activation_receipt_hash="c" * 64,
        activation_state_revision=2,
        effective_scope="project",
        instruction_materialization_id=(
            "distillation-instruction-materialization-"
            + hashlib.sha256(
                f"{receipt.source_import_id}:materialization".encode()
            ).hexdigest()[:32]
        ),
        instruction_materialization_hash="d" * 64,
        instruction_hash="e" * 64,
    )

    def runtime_bindings():
        return (
            DistillationSourceBinding(
                source_import_id=receipt.source_import_id,
                source_receipt_id=receipt.receipt_id,
                source_receipt_hash=receipt.receipt_hash,
                source_packet_fingerprint=receipt.source_packet_fingerprint,
                packet_text_hash=hashlib.sha256(b"alphabeta").hexdigest(),
            ),
            DistillationProjectBinding(
                project_id=project["project_id"],
                project_revision=project["revision"],
                project_fingerprint=project["fingerprint"],
            ),
            DistillationKnowledgeBinding(
                scope_id=scope["scope_id"],
                scope_state_revision=scope["state_revision"],
                knowledge_fingerprint=knowledge["fingerprint"],
                project_kb_state_id=None,
                project_kb_manifest_hash=None,
            ),
            instruction,
            DistillationProviderBinding(
                executor=ExecutorIdentity(
                    executor_id="executor.runtime-deep-test",
                    kind=ExecutorKind.EMBEDDED,
                    implementation="runtime-deep-test",
                    version="1",
                    protocol_version="1",
                    instance_id=None,
                ),
                capabilities=CapabilityProfile(
                    protocol_version="1",
                    names=[CapabilityName.STREAMING],
                    extensions={},
                ),
                requested_model="runtime-deep-test-model",
                accepted_reported_models=("runtime-deep-test-model",),
                dispatch_config_fingerprint="f" * 64,
                credential_reference=None,
                tool_definitions_hash="0" * 64,
            ),
        )

    monkeypatch.setattr(fixtures, "SOURCE_IMPORT_ID", receipt.source_import_id)
    monkeypatch.setattr(fixtures, "SOURCE_RECEIPT_ID", receipt.receipt_id)
    monkeypatch.setattr(fixtures, "PROJECT_ID", project["project_id"])
    monkeypatch.setattr(fixtures, "IDEMPOTENCY_KEY", idempotency_key)
    monkeypatch.setattr(fixtures, "_bindings", runtime_bindings)
    chain = fixtures._normal_chain()
    sqlite = client.app.state.runtime.store
    created_at = datetime.now(UTC).isoformat()
    with sqlite.connection() as connection:
        connection.execute(
            """
            INSERT OR IGNORE INTO skill_version(
                id, skill_id, version, source_path, fingerprint,
                manifest_fingerprint, snapshot_status, status, registered_at
            ) VALUES (?, ?, ?, ?, ?, ?, 'managed', 'active', ?)
            """,
            (
                instruction.skill_version_id,
                instruction.skill_id,
                instruction.skill_version,
                "adapters/builtin/novel-distillation-deep-instruction/skill.manifest.json",
                instruction.package_fingerprint,
                instruction.manifest_fingerprint,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO skill_activation_receipt(
                id, scope_key, scope_type, project_binding_id, skill_id, action,
                previous_state, previous_version_id, next_state, next_version_id,
                expected_state_revision, expected_version_id, state_revision,
                receipt_hash, payload_json, created_at
            ) VALUES (?, ?, 'project', ?, ?, 'enable', 'disabled', NULL,
                      'enabled', ?, ?, ?, ?, ?, '{}', ?)
            """,
            (
                instruction.activation_receipt_id,
                project["project_id"],
                project["project_id"],
                instruction.skill_id,
                instruction.skill_version_id,
                instruction.activation_state_revision - 1,
                instruction.skill_version_id,
                instruction.activation_state_revision,
                instruction.activation_receipt_hash,
                created_at,
            ),
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_source_scope(
                source_import_id, scope_id, bound_at
            ) VALUES (?, ?, ?)
            """,
            (receipt.source_import_id, scope["scope_id"], created_at),
        )
        connection.commit()

    invocation_store = DistillationInvocationStore(sqlite)
    invocation_store.create_prepared(
        receipt=chain["prepared"],
        idempotency_key=idempotency_key,
    )
    for key in ("dispatch1", "result1", "dispatch2", "result2", "candidate"):
        invocation_store.append_receipt(chain[key])
    final = fixtures._success_final(chain, ordinal=7, parent=chain["candidate"])
    invocation_store.append_receipt(final)
    return {
        "chain": chain,
        "final": final,
        "invocation_id": chain["catalog"].invocation_id,
        "scope": scope,
    }


def _review_promote_runtime_deep_batch(
    client: TestClient,
    *,
    knowledge_path: str,
    source_import_id: str,
    suffix: str = "finalization",
) -> dict:
    batch = client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert batch.status_code == 200, batch.text
    reviewed_cards = {
        "work": _reviewed_work_card(f"work-runtime-deep-{suffix}"),
        "voice": _reviewed_voice_card(f"voice-runtime-deep-{suffix}"),
        "validator": _reviewed_validator_card(f"validator-runtime-deep-{suffix}"),
    }
    for candidate in batch.json()["candidates"]:
        reviewed = client.put(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
                "card": reviewed_cards[candidate["card_type"]],
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        promoted = client.post(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}/promote",
            json={
                "expected_revision": reviewed.json()["revision"],
                "expected_content_hash": reviewed.json()["content_hash"],
                "replace_active": False,
            },
        )
        assert promoted.status_code == 200, promoted.text
    terminal = client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert terminal.status_code == 200, terminal.text
    return terminal.json()


def _reject_runtime_deep_batch(
    client: TestClient,
    *,
    knowledge_path: str,
    source_import_id: str,
) -> dict:
    batch = client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert batch.status_code == 200, batch.text
    for candidate in batch.json()["candidates"]:
        rejected = client.post(
            f"{knowledge_path}/candidates/{candidate['candidate_id']}/reject",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
            },
        )
        assert rejected.status_code == 200, rejected.text
    terminal = client.get(
        f"{knowledge_path}/candidates",
        params={"source_import_id": source_import_id},
    )
    assert terminal.status_code == 200, terminal.text
    return terminal.json()


def _import_candidate_batch(client: TestClient, source_import_id: str) -> dict:
    response = client.post(
        f"/api/v1/knowledge/source-imports/{source_import_id}/candidates"
    )
    assert response.status_code == 201, response.text
    return response.json()


def _reject_other_pending_candidates(
    client: TestClient,
    source_import_id: str,
    *,
    keep_card_ids: set[str] | None = None,
) -> dict:
    keep_card_ids = keep_card_ids or set()
    batch = client.get(
        "/api/v1/knowledge/candidates",
        params={"source_import_id": source_import_id},
    )
    assert batch.status_code == 200, batch.text
    for candidate in batch.json()["candidates"]:
        if candidate["status"] != "pending" or candidate["card_id"] in keep_card_ids:
            continue
        rejected = client.post(
            f"/api/v1/knowledge/candidates/{candidate['candidate_id']}/reject",
            json={
                "expected_revision": candidate["revision"],
                "expected_content_hash": candidate["content_hash"],
            },
        )
        assert rejected.status_code == 200, rejected.text
    terminal = client.get(
        "/api/v1/knowledge/candidates",
        params={"source_import_id": source_import_id},
    )
    assert terminal.status_code == 200, terminal.text
    return terminal.json()


def _review_and_promote_work_candidate(
    client: TestClient,
    source_import_id: str,
    batch: dict,
    *,
    card_id: str,
    promise: str,
    replace_active: bool,
) -> tuple[dict, dict]:
    pending = next(
        candidate
        for candidate in batch["candidates"]
        if candidate["card_id"] == card_id
    )
    reviewed = client.put(
        f"/api/v1/knowledge/candidates/{pending['candidate_id']}",
        json={
            "expected_revision": pending["revision"],
            "expected_content_hash": pending["content_hash"],
            "card": _reviewed_work_card(card_id, promise=promise),
        },
    )
    assert reviewed.status_code == 200, reviewed.text
    reviewed_candidate = reviewed.json()
    promoted = client.post(
        f"/api/v1/knowledge/candidates/{pending['candidate_id']}/promote",
        json={
            "expected_revision": reviewed_candidate["revision"],
            "expected_content_hash": reviewed_candidate["content_hash"],
            "replace_active": replace_active,
        },
    )
    assert promoted.status_code == 200, promoted.text
    terminal = _reject_other_pending_candidates(
        client,
        source_import_id,
        keep_card_ids={card_id},
    )
    return promoted.json(), terminal


def _finalize_source_batch(
    client: TestClient,
    source_import_id: str,
    receipt: SourceImportExecutionReceipt,
    candidate_set_hash: str,
):
    return client.post(
        f"/api/v1/knowledge/source-imports/{source_import_id}/finalize",
        json={
            "expected_source_receipt_hash": receipt.receipt_hash,
            "expected_draft_hash": receipt.cards_draft_hash,
            "expected_candidate_set_hash": candidate_set_hash,
        },
    )


def _assert_source_batch_not_formalized(
    store: SqliteStore,
    source_import_id: str,
) -> None:
    job = store.get_source_import_job(source_import_id)
    assert job is not None
    assert job["status"] == "awaiting_review"
    with store.connection() as connection:
        receipt_count = int(
            connection.execute(
                "SELECT COUNT(*) FROM knowledge_formalization_receipt "
                "WHERE source_import_id = ?",
                (source_import_id,),
            ).fetchone()[0]
        )
    assert receipt_count == 0


def _assert_source_scope_unbound(store: SqliteStore, source_import_id: str) -> None:
    with store.connection() as connection:
        assert connection.execute(
            "SELECT 1 FROM knowledge_source_scope WHERE source_import_id = ?",
            (source_import_id,),
        ).fetchone() is None


def test_project_batch_formalizes_work_voice_validator_into_context_and_nlcl(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / "typed-knowledge-novel")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    source_import_id = "source-import-" + "f" * 32
    formal_cards = [
        _reviewed_work_card("work-typed-context"),
        _reviewed_voice_card("voice-typed-context"),
        _reviewed_validator_card("validator-typed-context"),
    ]
    draft_cards = []
    for formal_card in formal_cards:
        draft = deepcopy(formal_card)
        draft["draft_candidate"] = True
        draft["requires_human_distillation"] = True
        draft_cards.append(draft)
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=source_import_id,
        card_id="unused-typed-card",
        cards_payload=draft_cards,
    )
    unformalized_import_id = "source-import-" + "e" * 32
    _seed_source_import(
        store,
        settings.database_path,
        import_id=unformalized_import_id,
        card_id="work-unformalized-selector",
    )

    with TestClient(create_app(settings)) as client:
        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        project_record = inspection.json()["project"]
        project_id = project_record["project_id"]
        knowledge_path = f"/api/v1/projects/{project_id}/knowledge"
        unformalized = client.post(
            f"{knowledge_path}/source-imports/{unformalized_import_id}/candidates"
        )
        assert unformalized.status_code == 201, unformalized.text

        imported = client.post(
            f"{knowledge_path}/source-imports/{source_import_id}/candidates"
        )
        assert imported.status_code == 201, imported.text
        imported_candidates = imported.json()["candidates"]
        assert {item["card_type"] for item in imported_candidates} == {
            "work",
            "voice",
            "validator",
        }
        assert {item["status"] for item in imported_candidates} == {"pending"}

        formal_by_id = {card["card_id"]: card for card in formal_cards}
        promoted_types: set[str] = set()
        for candidate in imported_candidates:
            reviewed = client.put(
                f"{knowledge_path}/candidates/{candidate['candidate_id']}",
                json={
                    "expected_revision": candidate["revision"],
                    "expected_content_hash": candidate["content_hash"],
                    "card": formal_by_id[candidate["card_id"]],
                },
            )
            assert reviewed.status_code == 200, reviewed.text
            assert reviewed.json()["issues"] == []
            promoted = client.post(
                f"{knowledge_path}/candidates/{candidate['candidate_id']}/promote",
                json={
                    "expected_revision": reviewed.json()["revision"],
                    "expected_content_hash": reviewed.json()["content_hash"],
                    "replace_active": False,
                },
            )
            assert promoted.status_code == 200, promoted.text
            promoted_types.add(promoted.json()["active_version"]["card"]["card_type"])
        assert promoted_types == {"work", "voice", "validator"}

        terminal_batch = client.get(
            f"{knowledge_path}/candidates",
            params={"source_import_id": source_import_id},
        ).json()
        finalized = client.post(
            f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
            json={
                "expected_source_receipt_hash": receipt.receipt_hash,
                "expected_draft_hash": receipt.cards_draft_hash,
                "expected_candidate_set_hash": terminal_batch["candidate_set_hash"],
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert {item["decision"] for item in finalized.json()["decisions"]} == {
            "promoted"
        }

        scoped_cards = client.get(f"{knowledge_path}/cards")
        assert scoped_cards.status_code == 200, scoped_cards.text
        assert {
            item["active_version"]["card"]["card_type"]
            for item in scoped_cards.json()["cards"]
        } == {"work", "voice", "validator"}
        candidates_by_id = {
            item["candidate_id"]: item for item in terminal_batch["candidates"]
        }
        cards_by_id = {
            item["card_id"]: item for item in scoped_cards.json()["cards"]
        }
        selector_cards = []
        for decision in finalized.json()["decisions"]:
            assert decision["decision"] == "promoted"
            candidate = candidates_by_id[decision["candidate_id"]]
            card = cards_by_id[candidate["card_id"]]
            version = next(
                item
                for item in card["versions"]
                if item["version_id"] == decision["promoted_version_id"]
            )
            selector_cards.append(
                {
                    "card_id": card["card_id"],
                    "version_id": version["version_id"],
                    "content_hash": version["content_hash"],
                }
            )
        selector_cards.sort(key=lambda item: item["card_id"])
        formal_selector = {
            "schema": "zhimeng.formal-knowledge-selector.v1",
            "source_import_id": source_import_id,
            "formalization_receipt_id": finalized.json()[
                "formalization_receipt_id"
            ],
            "formalization_receipt_hash": finalized.json()["receipt_hash"],
            "expected_knowledge_fingerprint": scoped_cards.json()["fingerprint"],
            "cards": selector_cards,
        }
        project_kb_binding = _publish_project_kb(client, project_record)

        agent_store = AgentStore(client.app.state.runtime.store)
        session = agent_store.create_session(
            project_id=project_id,
            title="Typed native knowledge Context",
            executor=ExecutorIdentity(
                executor_id="executor.typed-native-knowledge-test",
                kind=ExecutorKind.EMBEDDED,
                implementation="typed-native-knowledge-test",
                version="1",
                protocol_version="1",
                instance_id=None,
            ),
            capabilities=CapabilityProfile(
                protocol_version="1",
                names=[CapabilityName.STREAMING],
                extensions={},
            ),
        )
        context = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "typed-native-context-0001"},
            json={
                "objective": "按作者刚刚明确选择的资料批次准备正文修改候选。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
                **project_kb_binding,
                "formal_knowledge_selector": formal_selector,
            },
        )
        assert context.status_code == 201, context.text
        pack = context.json()["pack"]
        included_types = {
            item["source_type"].removeprefix("native_knowledge_card:")
            for item in pack["included"]
            if item["source_type"].startswith("native_knowledge_card:")
        }
        assert included_types == {"work", "voice", "validator"}
        assert pack["knowledge_fingerprint"] == scoped_cards.json()["fingerprint"]
        assert pack["formal_knowledge_selector"] == formal_selector
        assert all(
            item["mandatory"] is True
            for item in pack["included"]
            if item["source_type"].startswith("native_knowledge_card:")
        )

        route = client.get(
            f"/api/v1/agent/context-packs/{pack['context_pack_id']}/nlcl-route"
        )
        assert route.status_code == 200, route.text
        assert route.json()["source"] == "native_knowledge"
        assert {item["card_type"] for item in route.json()["selected_cards"]} == {
            "work",
            "voice",
            "validator",
        }
        assert all(
            item["knowledge_provenance"]["source_import_id"] == source_import_id
            for item in route.json()["selected_cards"]
        )
        replayed_selector = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "typed-native-context-0001"},
            json={
                "objective": "按作者刚刚明确选择的资料批次准备正文修改候选。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
                **project_kb_binding,
                "formal_knowledge_selector": formal_selector,
            },
        )
        assert replayed_selector.status_code == 201, replayed_selector.text
        assert replayed_selector.json()["pack"]["pack_hash"] == pack["pack_hash"]

        pack_model = ContextPack.model_validate(pack)
        changed_selector = FormalKnowledgeSelector.model_validate(
            {**formal_selector, "formalization_receipt_hash": "0" * 64}
        )
        assert context_pack_hash(
            pack_model.model_copy(
                update={"formal_knowledge_selector": changed_selector}
            )
        ) != pack["pack_hash"]
        replay_with_changed_selector = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "typed-native-context-0001"},
            json={
                "objective": "按作者刚刚明确选择的资料批次准备正文修改候选。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
                **project_kb_binding,
                "formal_knowledge_selector": {
                    **formal_selector,
                    "formalization_receipt_hash": "0" * 64,
                },
            },
        )
        assert replay_with_changed_selector.status_code == 409

        unformalized_selector = {
            "schema": "zhimeng.formal-knowledge-selector.v1",
            "source_import_id": unformalized_import_id,
            "formalization_receipt_id": knowledge_formalization_receipt_id(
                unformalized_import_id
            ),
            "formalization_receipt_hash": "1" * 64,
            "expected_knowledge_fingerprint": scoped_cards.json()["fingerprint"],
            "cards": [
                {
                    "card_id": "work-unformalized-selector",
                    "version_id": "knowledge-version-" + "1" * 32,
                    "content_hash": "2" * 64,
                }
            ],
        }
        unformalized_context = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "typed-native-context-unformalized"},
            json={
                "objective": "未正式化批次不能进入写作上下文。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
                **project_kb_binding,
                "formal_knowledge_selector": unformalized_selector,
            },
        )
        assert unformalized_context.status_code == 409, unformalized_context.text
        assert unformalized_context.json()["error"]["code"] == "context_conflict"

        for suffix, tampered_selector in (
            (
                "missing-card",
                {**formal_selector, "cards": formal_selector["cards"][:-1]},
            ),
            (
                "stale-card",
                {
                    **formal_selector,
                    "cards": [
                        {**formal_selector["cards"][0], "content_hash": "0" * 64},
                        *formal_selector["cards"][1:],
                    ],
                },
            ),
            (
                "stale-receipt",
                {**formal_selector, "formalization_receipt_hash": "0" * 64},
            ),
            (
                "stale-fingerprint",
                {**formal_selector, "expected_knowledge_fingerprint": "0" * 64},
            ),
        ):
            rejected = client.post(
                f"/api/v1/agent/sessions/{session.session_id}/context-packs",
                headers={"Idempotency-Key": f"typed-native-context-{suffix}"},
                json={
                    "objective": "同一批次的失败关闭验证。",
                    "profile": "chapter",
                    "active_chapter_id": "VOL001-CH001",
                    "expected_project_revision": project_record["revision"],
                    "expected_project_fingerprint": project_record["fingerprint"],
                    **project_kb_binding,
                    "formal_knowledge_selector": tampered_selector,
                },
            )
            assert rejected.status_code == 409, rejected.text
            assert rejected.json()["error"]["code"] == "context_conflict"

        client.app.state.context_service.max_characters = 2_000
        over_budget = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "typed-native-context-over-budget"},
            json={
                "objective": "预算不足时也不能静默丢失作者明确选择的知识。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_record["revision"],
                "expected_project_fingerprint": project_record["fingerprint"],
                **project_kb_binding,
                "formal_knowledge_selector": formal_selector,
            },
        )
        assert over_budget.status_code == 201, over_budget.text
        blocked_pack = over_budget.json()["pack"]
        assert blocked_pack["readiness"] == "blocked"
        excluded_by_id = {
            item["source_id"]: item for item in blocked_pack["excluded"]
        }
        assert {
            f"knowledge-card:{item['card_id']}" for item in formal_selector["cards"]
        }.issubset(excluded_by_id)
        assert all(
            excluded_by_id[f"knowledge-card:{item['card_id']}"]["mandatory"] is True
            for item in formal_selector["cards"]
        )


def test_runtime_deep_finalization_requires_final_invocation_evidence_and_keeps_legacy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / "runtime-deep-finalization")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    runtime_source_import_id = "source-import-" + "7" * 32
    runtime_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=runtime_source_import_id,
        card_id="unused-runtime-deep",
    )
    legacy_source_import_id = "source-import-" + "8" * 32
    legacy_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=legacy_source_import_id,
        card_id="work-legacy-runtime-deep-regression",
    )

    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        project_record = inspected.json()["project"]
        knowledge_path = f"/api/v1/projects/{project_record['project_id']}/knowledge"
        seeded = _seed_runtime_deep_invocation(
            client,
            monkeypatch,
            receipt=runtime_receipt,
            project=project_record,
            knowledge_path=knowledge_path,
        )
        terminal = _review_promote_runtime_deep_batch(
            client,
            knowledge_path=knowledge_path,
            source_import_id=runtime_source_import_id,
        )
        finalized = client.post(
            f"{knowledge_path}/source-imports/{runtime_source_import_id}/formalization/finalize",
            json={
                "expected_source_receipt_hash": runtime_receipt.receipt_hash,
                "expected_draft_hash": terminal["candidates"][0]["source_draft_hash"],
                "expected_candidate_set_hash": terminal["candidate_set_hash"],
            },
        )
        assert finalized.status_code == 200, finalized.text
        assert finalized.json()["schema"] == "zhimeng.knowledge-formalization-receipt.v2"
        assert {item["decision"] for item in finalized.json()["decisions"]} == {
            "promoted"
        }
        assert finalized.json()["cards_draft_hash"] == seeded["chain"][
            "candidate_set"
        ].candidate_set_hash

        # Legacy source-import formalization still uses the original cards draft
        # receipt and is not forced through a Runtime-deep final result.
        imported = client.post(
            f"/api/v1/knowledge/source-imports/{legacy_source_import_id}/candidates"
        )
        assert imported.status_code == 201, imported.text
        promoted, legacy_terminal = _review_and_promote_work_candidate(
            client,
            legacy_source_import_id,
            imported.json(),
            card_id="work-legacy-runtime-deep-regression",
            promise="Legacy import still formalizes from the original source draft without a Runtime-deep invocation.",
            replace_active=False,
        )
        assert promoted["active_version"]["card"]["card_id"] == (
            "work-legacy-runtime-deep-regression"
        )
        legacy_finalized = client.post(
            f"/api/v1/knowledge/source-imports/{legacy_source_import_id}/finalize",
            json={
                "expected_source_receipt_hash": legacy_receipt.receipt_hash,
                "expected_draft_hash": legacy_receipt.cards_draft_hash,
                "expected_candidate_set_hash": legacy_terminal["candidate_set_hash"],
            },
        )
        assert legacy_finalized.status_code == 200, legacy_finalized.text


@pytest.mark.parametrize(
    ("case_name", "tamper"),
    [
        ("wrong-invocation-source", "invocation_source"),
        ("candidate-incomplete", "candidate_incomplete"),
        ("candidate-invocation-null", "candidate_invocation_null"),
        ("candidate-other-invocation", "candidate_other_invocation"),
        ("type-order-wrong", "type_order_wrong"),
        ("mixed-candidate-hash", "mixed_candidate_hash"),
        ("provider-receipt-tampered", "provider_receipt_tampered"),
        ("provider-receipt-missing", "provider_receipt_missing"),
        ("final-receipt-tampered", "final_receipt_tampered"),
        ("final-receipt-missing", "final_receipt_missing"),
        ("candidate-output-tampered", "candidate_output_tampered"),
        ("candidate-output-receipt-missing", "candidate_output_receipt_missing"),
        (
            "candidate-output-pointer-hash-missing",
            "candidate_output_pointer_hash_missing",
        ),
    ],
)
def test_runtime_deep_finalization_rejects_corrupt_invocation_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    case_name: str,
    tamper: str,
) -> None:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / f"runtime-deep-negative-{case_name}")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    source_import_id = (
        "source-import-"
        + hashlib.sha256(case_name.encode()).hexdigest()[:32]
    )
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=source_import_id,
        card_id=f"unused-{case_name}",
    )

    with TestClient(create_app(settings)) as client:
        inspected = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspected.status_code == 200, inspected.text
        project_record = inspected.json()["project"]
        knowledge_path = f"/api/v1/projects/{project_record['project_id']}/knowledge"
        seeded = _seed_runtime_deep_invocation(
            client,
            monkeypatch,
            receipt=receipt,
            project=project_record,
            knowledge_path=knowledge_path,
        )
        terminal = _reject_runtime_deep_batch(
            client,
            knowledge_path=knowledge_path,
            source_import_id=source_import_id,
        )
        invocation_id = seeded["invocation_id"]
        with client.app.state.runtime.store.connection() as connection:
            if tamper == "invocation_source":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_identity_immutable"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_state_transition"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_receipt_binding"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_success_contract"
                )
                connection.execute(
                    """
                    UPDATE distillation_invocation
                    SET source_receipt_hash = ?
                    WHERE id = ?
                    """,
                    ("0" * 64, invocation_id),
                )
            elif tamper == "candidate_incomplete":
                connection.execute(
                    """
                    DELETE FROM knowledge_candidate
                    WHERE runtime_distillation_invocation_id = ?
                      AND source_card_index = 2
                    """,
                    (invocation_id,),
                )
            elif tamper == "candidate_invocation_null":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_knowledge_candidate_distillation_binding_immutable"
                )
                connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET runtime_distillation_invocation_id = NULL
                    WHERE runtime_distillation_invocation_id = ?
                      AND source_card_index = 1
                    """,
                    (invocation_id,),
                )
            elif tamper == "candidate_other_invocation":
                other_invocation_id = (
                    "distillation-invocation-"
                    + hashlib.sha256(f"{case_name}:other-invocation".encode()).hexdigest()[
                        :32
                    ]
                )
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_knowledge_candidate_distillation_binding_immutable"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_knowledge_candidate_distillation_update"
                )
                connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET runtime_distillation_invocation_id = ?
                    WHERE runtime_distillation_invocation_id = ?
                      AND source_card_index = 1
                    """,
                    (other_invocation_id, invocation_id),
                )
            elif tamper == "type_order_wrong":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_update_immutable"
                )
                row = connection.execute(
                    """
                    SELECT id, payload_json FROM distillation_invocation_receipt
                    WHERE invocation_id = ? AND kind = 'final_result'
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                payload = json.loads(str(row["payload_json"]))
                candidates = payload["payload"]["candidates"]
                candidates[0], candidates[1] = candidates[1], candidates[0]
                connection.execute(
                    """
                    UPDATE distillation_invocation_receipt
                    SET payload_json = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        row["id"],
                    ),
                )
            elif tamper == "mixed_candidate_hash":
                connection.execute(
                    """
                    UPDATE knowledge_candidate
                    SET source_draft_hash = ?
                    WHERE runtime_distillation_invocation_id = ?
                      AND source_card_index = 1
                    """,
                    ("f" * 64, invocation_id),
                )
            elif tamper == "provider_receipt_tampered":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_update_immutable"
                )
                row = connection.execute(
                    """
                    SELECT id FROM distillation_invocation_receipt
                    WHERE invocation_id = ? AND kind = 'provider_turn_result'
                    ORDER BY ordinal DESC
                    LIMIT 1
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                connection.execute(
                    """
                    UPDATE distillation_invocation_receipt
                    SET receipt_hash = ?
                    WHERE id = ?
                    """,
                    ("0" * 64, row["id"]),
                )
            elif tamper == "provider_receipt_missing":
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_delete_immutable"
                )
                row = connection.execute(
                    """
                    SELECT last_turn_result_receipt_id AS id
                    FROM distillation_invocation
                    WHERE id = ?
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                connection.execute(
                    "DELETE FROM distillation_invocation_receipt WHERE id = ?",
                    (row["id"],),
                )
            elif tamper == "final_receipt_tampered":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_state_transition"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_receipt_binding"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_success_contract"
                )
                connection.execute(
                    """
                    UPDATE distillation_invocation
                    SET final_result_receipt_hash = ?
                    WHERE id = ?
                    """,
                    ("0" * 64, invocation_id),
                )
            elif tamper == "final_receipt_missing":
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_delete_immutable"
                )
                row = connection.execute(
                    """
                    SELECT final_result_receipt_id AS id
                    FROM distillation_invocation
                    WHERE id = ?
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                connection.execute(
                    "DELETE FROM distillation_invocation_receipt WHERE id = ?",
                    (row["id"],),
                )
            elif tamper == "candidate_output_tampered":
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_update_immutable"
                )
                row = connection.execute(
                    """
                    SELECT id, payload_json FROM distillation_invocation_receipt
                    WHERE invocation_id = ? AND kind = 'candidate_output'
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                payload = json.loads(str(row["payload_json"]))
                payload["payload"]["candidate_set"]["candidate_set_hash"] = "0" * 64
                connection.execute(
                    """
                    UPDATE distillation_invocation_receipt
                    SET payload_json = ?
                    WHERE id = ?
                    """,
                    (
                        json.dumps(
                            payload,
                            ensure_ascii=False,
                            sort_keys=True,
                            separators=(",", ":"),
                        ),
                        row["id"],
                    ),
                )
            elif tamper == "candidate_output_receipt_missing":
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_receipt_delete_immutable"
                )
                row = connection.execute(
                    """
                    SELECT candidate_output_receipt_id AS id
                    FROM distillation_invocation
                    WHERE id = ?
                    """,
                    (invocation_id,),
                ).fetchone()
                assert row is not None
                connection.execute(
                    "DELETE FROM distillation_invocation_receipt WHERE id = ?",
                    (row["id"],),
                )
            elif tamper == "candidate_output_pointer_hash_missing":
                connection.execute("PRAGMA ignore_check_constraints = ON")
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_state_transition"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_receipt_binding"
                )
                connection.execute(
                    "DROP TRIGGER IF EXISTS trg_distillation_invocation_success_contract"
                )
                connection.execute(
                    """
                    UPDATE distillation_invocation
                    SET candidate_output_receipt_id = NULL,
                        candidate_output_receipt_hash = NULL
                    WHERE id = ?
                    """,
                    (invocation_id,),
                )
            connection.commit()

        response = client.post(
            f"{knowledge_path}/source-imports/{source_import_id}/formalization/finalize",
            json={
                "expected_source_receipt_hash": receipt.receipt_hash,
                "expected_draft_hash": terminal["candidates"][0]["source_draft_hash"],
                "expected_candidate_set_hash": terminal["candidate_set_hash"],
            },
        )
        assert response.status_code == 409, response.text
        _assert_source_batch_not_formalized(
            client.app.state.runtime.store,
            source_import_id,
        )


def test_native_knowledge_review_promotion_fingerprint_and_state_controls(
    tmp_path: Path,
) -> None:
    projects_root = tmp_path / "projects"
    project = _write_project(projects_root / "novel")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    first_import = "source-import-" + "1" * 32
    card_id = "work-native-knowledge-test"
    source_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=first_import,
        card_id=card_id,
    )

    with TestClient(create_app(settings)) as client:
        imported = client.post(
            f"/api/v1/knowledge/source-imports/{first_import}/candidates"
        )
        replayed = client.post(
            f"/api/v1/knowledge/source-imports/{first_import}/candidates"
        )
        assert imported.status_code == 201, imported.text
        assert replayed.status_code == 201, replayed.text
        candidates = imported.json()["candidates"]
        assert [item["status"] for item in candidates] == ["pending", "pending", "pending"]
        assert [item["candidate_id"] for item in replayed.json()["candidates"]] == [
            item["candidate_id"] for item in candidates
        ]
        pending = candidates[0]
        assert pending["issues"]

        for support_candidate in candidates[1:]:
            rejected_support = client.post(
                f"/api/v1/knowledge/candidates/{support_candidate['candidate_id']}/reject",
                json={
                    "expected_revision": support_candidate["revision"],
                    "expected_content_hash": support_candidate["content_hash"],
                },
            )
            assert rejected_support.status_code == 200, rejected_support.text

        reviewed_card = _reviewed_work_card(card_id)
        updated = client.put(
            f"/api/v1/knowledge/candidates/{pending['candidate_id']}",
            json={
                "expected_revision": pending["revision"],
                "expected_content_hash": pending["content_hash"],
                "card": reviewed_card,
            },
        )
        assert updated.status_code == 200, updated.text
        reviewed = updated.json()
        assert reviewed["revision"] == 2
        assert reviewed["issues"] == []

        stale = client.put(
            f"/api/v1/knowledge/candidates/{pending['candidate_id']}",
            json={
                "expected_revision": pending["revision"],
                "expected_content_hash": pending["content_hash"],
                "card": reviewed_card,
            },
        )
        assert stale.status_code == 409

        promoted = client.post(
            f"/api/v1/knowledge/candidates/{pending['candidate_id']}/promote",
            json={
                "expected_revision": reviewed["revision"],
                "expected_content_hash": reviewed["content_hash"],
                "replace_active": False,
            },
        )
        assert promoted.status_code == 200, promoted.text
        card = promoted.json()
        assert card["status"] == "active"
        assert card["active_version"]["version_number"] == 1
        candidates_after_promotion = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": first_import},
        ).json()
        finalized = client.post(
            f"/api/v1/knowledge/source-imports/{first_import}/finalize",
            json={
                "expected_source_receipt_hash": source_receipt.receipt_hash,
                "expected_draft_hash": source_receipt.cards_draft_hash,
                "expected_candidate_set_hash": candidates_after_promotion[
                    "candidate_set_hash"
                ],
            },
        )
        assert finalized.status_code == 200, finalized.text

        inspection = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project)},
        )
        assert inspection.status_code == 200, inspection.text
        project_payload = inspection.json()["project"]
        project_kb_binding = _publish_project_kb(client, project_payload)
        agent_store = AgentStore(client.app.state.runtime.store)
        session = agent_store.create_session(
            project_id=project_payload["project_id"],
            title="Native knowledge Context",
            executor=ExecutorIdentity(
                executor_id="executor.native-knowledge-test",
                kind=ExecutorKind.EMBEDDED,
                implementation="native-knowledge-test",
                version="1",
                protocol_version="1",
                instance_id=None,
            ),
            capabilities=CapabilityProfile(
                protocol_version="1",
                names=[CapabilityName.STREAMING],
                extensions={},
            ),
        )
        context = client.post(
            f"/api/v1/agent/sessions/{session.session_id}/context-packs",
            headers={"Idempotency-Key": "native-knowledge-context-0001"},
            json={
                "objective": "玄幻升级章节要完成阶段兑现并留下更高层债务。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_payload["revision"],
                "expected_project_fingerprint": project_payload["fingerprint"],
                **project_kb_binding,
            },
        )
        assert context.status_code == 201, context.text
        pack = context.json()["pack"]
        assert pack["knowledge_fingerprint"]
        assert any(
            item["source_id"] == f"knowledge-card:{card_id}"
            and item["source_type"] == "native_knowledge_card:work"
            for item in pack["included"]
        )

        listed = client.get("/api/v1/knowledge/cards")
        assert listed.status_code == 200, listed.text
        first_fingerprint = listed.json()["fingerprint"]

        disabled = client.post(
            f"/api/v1/knowledge/cards/{card_id}/disable",
            json={
                "expected_state_revision": card["state_revision"],
                "expected_active_version_id": card["active_version_id"],
            },
        )
        assert disabled.status_code == 200, disabled.text
        assert disabled.json()["status"] == "disabled"
        disabled_fingerprint = client.get("/api/v1/knowledge/cards").json()["fingerprint"]
        assert disabled_fingerprint != first_fingerprint

        disabled_card = disabled.json()
        enabled = client.post(
            f"/api/v1/knowledge/cards/{card_id}/enable",
            json={
                "expected_state_revision": disabled_card["state_revision"],
                "expected_active_version_id": disabled_card["active_version_id"],
            },
        )
        assert enabled.status_code == 200, enabled.text
        assert enabled.json()["status"] == "active"

    service = KnowledgeService(store, settings.database_path.parent / "source-imports")
    selection = service.select_for_context("玄幻升级章节要完成阶段兑现", "chapter")
    assert selection.cards
    assert selection.cards[0].card_id == card_id
    assert selection.cards[0].route_terms == ("玄幻", "升级", "阶段兑现")
    assert "chapter" in selection.cards[0].recommended_profiles
    assert selection.cards[0].negative_routes == ("纯日常",)
    assert selection.cards[0].safety_summary == "只使用抽象推进机制，不使用来源人物和设定壳。"
    assert selection.cards[0].feedback_layer == "L3_DISTILLATION_RETRIEVAL"
    assert selection.fingerprint == service.observe_knowledge_fingerprint()


def test_native_knowledge_requires_explicit_replace_and_supports_rollback(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    card_id = "work-native-version-test"
    imports = ["source-import-" + value * 32 for value in ("2", "3")]
    source_receipts = {}
    for source_import_id in imports:
        source_receipts[source_import_id] = _seed_source_import(
            store,
            settings.database_path,
            import_id=source_import_id,
            card_id=card_id,
        )

    with TestClient(create_app(settings)) as client:
        promoted_cards = []
        for index, source_import_id in enumerate(imports):
            imported = client.post(
                f"/api/v1/knowledge/source-imports/{source_import_id}/candidates"
            ).json()["candidates"][0]
            reviewed_card = _reviewed_work_card(
                card_id,
                promise=(
                    "持续升级的危机、选择、代价与阶段兑现，并在每次回报后"
                    f"留下更高层债务；当前为复核版本 {index + 1}"
                ),
            )
            reviewed = client.put(
                f"/api/v1/knowledge/candidates/{imported['candidate_id']}",
                json={
                    "expected_revision": imported["revision"],
                    "expected_content_hash": imported["content_hash"],
                    "card": reviewed_card,
                },
            ).json()
            response = client.post(
                f"/api/v1/knowledge/candidates/{imported['candidate_id']}/promote",
                json={
                    "expected_revision": reviewed["revision"],
                    "expected_content_hash": reviewed["content_hash"],
                    "replace_active": False,
                },
            )
            if index == 1:
                assert response.status_code == 409
                response = client.post(
                    f"/api/v1/knowledge/candidates/{imported['candidate_id']}/promote",
                    json={
                        "expected_revision": reviewed["revision"],
                        "expected_content_hash": reviewed["content_hash"],
                        "replace_active": True,
                    },
                )
            assert response.status_code == 200, response.text
            batch = _reject_other_pending_candidates(
                client,
                source_import_id,
                keep_card_ids={card_id},
            )
            source_receipt = source_receipts[source_import_id]
            finalized = client.post(
                f"/api/v1/knowledge/source-imports/{source_import_id}/finalize",
                json={
                    "expected_source_receipt_hash": source_receipt.receipt_hash,
                    "expected_draft_hash": source_receipt.cards_draft_hash,
                    "expected_candidate_set_hash": batch["candidate_set_hash"],
                },
            )
            assert finalized.status_code == 200, finalized.text
            promoted_cards.append(client.get("/api/v1/knowledge/cards").json()["cards"][0])

        current = promoted_cards[-1]
        assert current["active_version"]["version_number"] == 2
        first_version_id = current["versions"][-1]["version_id"]
        rolled_back = client.post(
            f"/api/v1/knowledge/cards/{card_id}/rollback",
            json={
                "expected_state_revision": current["state_revision"],
                "expected_active_version_id": current["active_version_id"],
                "target_version_id": first_version_id,
            },
        )
        assert rolled_back.status_code == 200, rolled_back.text
        assert rolled_back.json()["active_version_id"] == first_version_id
        assert rolled_back.json()["active_version"]["version_number"] == 1


def test_native_knowledge_candidate_import_is_atomic_for_invalid_batches(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    import_id = "source-import-" + "4" * 32
    cards: list[object] = _draft_cards("work-native-atomic-test")[:1]
    cards.append("invalid-card")
    _seed_source_import(
        store,
        settings.database_path,
        import_id=import_id,
        card_id="work-native-atomic-test",
        cards_payload=cards,
    )

    with TestClient(create_app(settings)) as client:
        response = client.post(
            f"/api/v1/knowledge/source-imports/{import_id}/candidates"
        )
        assert response.status_code == 422, response.text
        listed = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": import_id},
        )
        assert listed.status_code == 200, listed.text
        assert listed.json()["candidates"] == []
        _assert_source_scope_unbound(store, import_id)


def test_staged_replacement_is_invisible_and_cannot_be_activated_before_finalize(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    card_id = "work-staged-replacement-test"
    base_import_id = "source-import-" + "5" * 32
    replacement_import_id = "source-import-" + "6" * 32
    staged_new_import_id = "source-import-" + "7" * 32
    base_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=base_import_id,
        card_id=card_id,
    )
    replacement_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=replacement_import_id,
        card_id=card_id,
    )
    staged_new_card_id = "work-unformalized-enable-test"
    _seed_source_import(
        store,
        settings.database_path,
        import_id=staged_new_import_id,
        card_id=staged_new_card_id,
    )

    with TestClient(create_app(settings)) as client:
        base_batch = _import_candidate_batch(client, base_import_id)
        _, base_batch = _review_and_promote_work_candidate(
            client,
            base_import_id,
            base_batch,
            card_id=card_id,
            promise="基础正式版本在每次兑现后留下可追溯的新债务和下一步选择压力。",
            replace_active=False,
        )
        finalized_base = _finalize_source_batch(
            client,
            base_import_id,
            base_receipt,
            base_batch["candidate_set_hash"],
        )
        assert finalized_base.status_code == 200, finalized_base.text
        base_cards = client.get("/api/v1/knowledge/cards").json()
        base_card = next(card for card in base_cards["cards"] if card["card_id"] == card_id)
        base_version_id = base_card["active_version_id"]
        base_state_revision = base_card["state_revision"]
        base_fingerprint = base_cards["fingerprint"]

        replacement_batch = _import_candidate_batch(client, replacement_import_id)
        staged_card, replacement_batch = _review_and_promote_work_candidate(
            client,
            replacement_import_id,
            replacement_batch,
            card_id=card_id,
            promise="替换候选只有整批正式化后才能改变现行版本和正式知识指纹。",
            replace_active=True,
        )
        staged_candidate = next(
            item
            for item in replacement_batch["candidates"]
            if item["card_id"] == card_id
        )
        staged_version_id = staged_candidate["promoted_version_id"]
        staged_version = next(
            version
            for version in staged_card["versions"]
            if version["version_id"] == staged_version_id
        )

        assert staged_card["active_version_id"] == base_version_id
        assert staged_card["state_revision"] == base_state_revision
        assert staged_version["formalization_status"] == "awaiting_formalization"
        assert client.get("/api/v1/knowledge/cards").json()["fingerprint"] == (
            base_fingerprint
        )

        rollback = client.post(
            f"/api/v1/knowledge/cards/{card_id}/rollback",
            json={
                "expected_state_revision": base_state_revision,
                "expected_active_version_id": base_version_id,
                "target_version_id": staged_version_id,
            },
        )
        assert rollback.status_code == 409, rollback.text

        staged_new_batch = _import_candidate_batch(client, staged_new_import_id)
        staged_new_card, _ = _review_and_promote_work_candidate(
            client,
            staged_new_import_id,
            staged_new_batch,
            card_id=staged_new_card_id,
            promise="首次晋升也必须等待批次正式化，不能通过启用接口提前进入正式知识。",
            replace_active=False,
        )
        enable = client.post(
            f"/api/v1/knowledge/cards/{staged_new_card_id}/enable",
            json={
                "expected_state_revision": staged_new_card["state_revision"],
                "expected_active_version_id": staged_new_card["active_version_id"],
            },
        )
        assert enable.status_code == 409, enable.text
        assert client.get("/api/v1/knowledge/cards").json()["fingerprint"] == (
            base_fingerprint
        )

        finalized_replacement = _finalize_source_batch(
            client,
            replacement_import_id,
            replacement_receipt,
            replacement_batch["candidate_set_hash"],
        )
        assert finalized_replacement.status_code == 200, finalized_replacement.text
        current_cards = client.get("/api/v1/knowledge/cards").json()
        current = next(card for card in current_cards["cards"] if card["card_id"] == card_id)
        assert current["active_version_id"] == staged_version_id
        assert current["state_revision"] == base_state_revision + 1
        assert current["active_version"]["formalization_status"] == "verified"
        assert current_cards["fingerprint"] != base_fingerprint


def test_finalize_requires_terminal_candidates_and_current_candidate_set(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    pending_import_id = "source-import-" + "8" * 32
    stale_import_id = "source-import-" + "9" * 32
    unbound_import_id = "source-import-" + "7" * 32
    pending_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=pending_import_id,
        card_id="work-pending-finalize-test",
    )
    stale_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=stale_import_id,
        card_id="work-stale-candidate-set-test",
    )
    unbound_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=unbound_import_id,
        card_id="work-unbound-finalize-test",
    )

    with TestClient(create_app(settings)) as client:
        unbound = _finalize_source_batch(
            client,
            unbound_import_id,
            unbound_receipt,
            "0" * 64,
        )
        assert unbound.status_code == 409, unbound.text
        _assert_source_scope_unbound(store, unbound_import_id)

        pending_batch = _import_candidate_batch(client, pending_import_id)
        pending = _finalize_source_batch(
            client,
            pending_import_id,
            pending_receipt,
            pending_batch["candidate_set_hash"],
        )
        assert pending.status_code == 409, pending.text
        _assert_source_batch_not_formalized(store, pending_import_id)

        stale_batch = _import_candidate_batch(client, stale_import_id)
        stale_candidate_set_hash = stale_batch["candidate_set_hash"]
        _, current_batch = _review_and_promote_work_candidate(
            client,
            stale_import_id,
            stale_batch,
            card_id="work-stale-candidate-set-test",
            promise="候选集合发生任何状态或内容变化后，旧集合哈希都必须失效。",
            replace_active=False,
        )
        assert current_batch["candidate_set_hash"] != stale_candidate_set_hash
        stale = _finalize_source_batch(
            client,
            stale_import_id,
            stale_receipt,
            stale_candidate_set_hash,
        )
        assert stale.status_code == 409, stale.text
        _assert_source_batch_not_formalized(store, stale_import_id)


@pytest.mark.parametrize(
    ("artifact_kind", "mutation"),
    [
        ("cards_draft_json", "tamper"),
        ("cards_draft_json", "delete"),
        ("packet_manifest", "tamper"),
        ("packet_manifest", "delete"),
        ("tool_result_envelope_json", "tamper"),
        ("tool_result_envelope_json", "delete"),
    ],
)
def test_finalize_rechecks_source_artifacts_without_partial_commit(
    tmp_path: Path,
    artifact_kind: str,
    mutation: str,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    import_id = "source-import-" + "a" * 32
    card_id = "work-artifact-recheck-test"
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=import_id,
        card_id=card_id,
    )

    with TestClient(create_app(settings)) as client:
        batch = _import_candidate_batch(client, import_id)
        _, batch = _review_and_promote_work_candidate(
            client,
            import_id,
            batch,
            card_id=card_id,
            promise="正式化必须重新核对源包、候选卡和工具 envelope 的实际磁盘字节。",
            replace_active=False,
        )
        evidence = next(
            artifact
            for artifact in (*receipt.source_artifacts, *receipt.draft_artifacts)
            if artifact.kind == artifact_kind
        )
        artifact_path = (
            settings.database_path.parent
            / "source-imports"
            / Path(evidence.relative_path)
        )
        if mutation == "delete":
            artifact_path.unlink()
        else:
            artifact_path.write_bytes(artifact_path.read_bytes() + b"\ntampered")

        response = _finalize_source_batch(
            client,
            import_id,
            receipt,
            batch["candidate_set_hash"],
        )
        assert response.status_code == 409, response.text
        _assert_source_batch_not_formalized(store, import_id)


def test_finalize_is_idempotent_for_same_material_and_conflicts_for_different_material(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    import_id = "source-import-" + "b" * 32
    card_id = "work-finalize-idempotency-test"
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=import_id,
        card_id=card_id,
    )

    with TestClient(create_app(settings)) as client:
        batch = _import_candidate_batch(client, import_id)
        _, batch = _review_and_promote_work_candidate(
            client,
            import_id,
            batch,
            card_id=card_id,
            promise="同一正式化请求可以安全重放，不同材料不能借用已有成功收据。",
            replace_active=False,
        )
        first = _finalize_source_batch(
            client,
            import_id,
            receipt,
            batch["candidate_set_hash"],
        )
        replay = _finalize_source_batch(
            client,
            import_id,
            receipt,
            batch["candidate_set_hash"],
        )
        assert first.status_code == 200, first.text
        assert replay.status_code == 200, replay.text
        assert replay.json() == first.json()

        different_hash = (
            ("0" if batch["candidate_set_hash"][0] != "0" else "1")
            + batch["candidate_set_hash"][1:]
        )
        conflict = _finalize_source_batch(
            client,
            import_id,
            receipt,
            different_hash,
        )
        assert conflict.status_code == 409, conflict.text
        with store.connection() as connection:
            receipt_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM knowledge_formalization_receipt "
                    "WHERE source_import_id = ?",
                    (import_id,),
                ).fetchone()[0]
            )
        assert receipt_count == 1
        assert store.get_source_import_job(import_id)["status"] == "completed"


def test_competing_staged_replacements_use_activation_compare_and_swap(
    tmp_path: Path,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    card_id = "work-activation-cas-test"
    base_import_id = "source-import-" + "c" * 32
    first_import_id = "source-import-" + "d" * 32
    second_import_id = "source-import-" + "e" * 32
    receipts = {
        source_import_id: _seed_source_import(
            store,
            settings.database_path,
            import_id=source_import_id,
            card_id=card_id,
        )
        for source_import_id in (base_import_id, first_import_id, second_import_id)
    }

    with TestClient(create_app(settings)) as client:
        base_batch = _import_candidate_batch(client, base_import_id)
        _, base_batch = _review_and_promote_work_candidate(
            client,
            base_import_id,
            base_batch,
            card_id=card_id,
            promise="并发替换测试的基础正式版本保留稳定的现行指针。",
            replace_active=False,
        )
        base_finalized = _finalize_source_batch(
            client,
            base_import_id,
            receipts[base_import_id],
            base_batch["candidate_set_hash"],
        )
        assert base_finalized.status_code == 200, base_finalized.text
        base_card = client.get("/api/v1/knowledge/cards").json()["cards"][0]
        base_version_id = base_card["active_version_id"]

        staged: dict[str, tuple[dict, str]] = {}
        for ordinal, source_import_id in enumerate(
            (first_import_id, second_import_id),
            start=1,
        ):
            batch = _import_candidate_batch(client, source_import_id)
            card, batch = _review_and_promote_work_candidate(
                client,
                source_import_id,
                batch,
                card_id=card_id,
                promise=(
                    "两个并发替换都必须绑定同一基础现行版本，只有先提交者可激活；"
                    f"这是候选 {ordinal}。"
                ),
                replace_active=True,
            )
            assert card["active_version_id"] == base_version_id
            candidate = next(
                item for item in batch["candidates"] if item["card_id"] == card_id
            )
            staged[source_import_id] = (batch, candidate["promoted_version_id"])

        first_batch, first_version_id = staged[first_import_id]
        first = _finalize_source_batch(
            client,
            first_import_id,
            receipts[first_import_id],
            first_batch["candidate_set_hash"],
        )
        assert first.status_code == 200, first.text

        second_batch, second_version_id = staged[second_import_id]
        second = _finalize_source_batch(
            client,
            second_import_id,
            receipts[second_import_id],
            second_batch["candidate_set_hash"],
        )
        assert second.status_code == 409, second.text
        _assert_source_batch_not_formalized(store, second_import_id)

        current = client.get("/api/v1/knowledge/cards").json()["cards"][0]
        assert current["active_version_id"] == first_version_id
        assert current["active_version_id"] != second_version_id
        assert current["active_version"]["formalization_status"] == "verified"


@pytest.mark.parametrize("with_base", [False, True], ids=("initial", "replacement"))
def test_rejected_staged_version_is_abandoned_and_same_card_can_be_promoted_again(
    tmp_path: Path,
    with_base: bool,
) -> None:
    settings = _settings(tmp_path)
    store = SqliteStore(settings.database_path)
    store.initialize()
    card_id = "work-rejected-staged-recovery"
    base_import_id = "source-import-" + "1" * 32
    rejected_import_id = "source-import-" + "2" * 32
    retry_import_id = "source-import-" + "3" * 32

    base_receipt = None
    if with_base:
        base_receipt = _seed_source_import(
            store,
            settings.database_path,
            import_id=base_import_id,
            card_id=card_id,
        )
    rejected_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=rejected_import_id,
        card_id=card_id,
    )
    retry_receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=retry_import_id,
        card_id=card_id,
    )

    with TestClient(create_app(settings)) as client:
        if base_receipt is not None:
            base_batch = _import_candidate_batch(client, base_import_id)
            _, base_batch = _review_and_promote_work_candidate(
                client,
                base_import_id,
                base_batch,
                card_id=card_id,
                promise="基础版本已经完整正式化，并保留稳定的激活基线。",
                replace_active=False,
            )
            finalized_base = _finalize_source_batch(
                client,
                base_import_id,
                base_receipt,
                base_batch["candidate_set_hash"],
            )
            assert finalized_base.status_code == 200, finalized_base.text

        baseline_cards = client.get("/api/v1/knowledge/cards")
        assert baseline_cards.status_code == 200, baseline_cards.text
        baseline_fingerprint = baseline_cards.json()["fingerprint"]

        rejected_batch = _import_candidate_batch(client, rejected_import_id)
        staged_card, rejected_batch = _review_and_promote_work_candidate(
            client,
            rejected_import_id,
            rejected_batch,
            card_id=card_id,
            promise="这个 staged 版本会被明确拒绝，但审计历史必须保留。",
            replace_active=with_base,
        )
        staged_candidate = next(
            item for item in rejected_batch["candidates"] if item["card_id"] == card_id
        )
        staged_version_id = staged_candidate["promoted_version_id"]
        rejected = client.post(
            f"/api/v1/knowledge/candidates/{staged_candidate['candidate_id']}/reject",
            json={
                "expected_revision": staged_candidate["revision"],
                "expected_content_hash": staged_candidate["content_hash"],
            },
        )
        assert rejected.status_code == 200, rejected.text
        assert rejected.json()["status"] == "rejected"
        assert rejected.json()["promoted_version_id"] is None

        staged_listing = client.get("/api/v1/knowledge/cards")
        assert staged_listing.status_code == 200, staged_listing.text
        staged_record = next(
            card for card in staged_listing.json()["cards"] if card["card_id"] == card_id
        )
        abandoned = next(
            version
            for version in staged_record["versions"]
            if version["version_id"] == staged_version_id
        )
        assert abandoned["formalization_status"] == "abandoned"
        assert staged_listing.json()["fingerprint"] == baseline_fingerprint

        rejected_batch = client.get(
            "/api/v1/knowledge/candidates",
            params={"source_import_id": rejected_import_id},
        ).json()
        finalized_rejection = _finalize_source_batch(
            client,
            rejected_import_id,
            rejected_receipt,
            rejected_batch["candidate_set_hash"],
        )
        assert finalized_rejection.status_code == 200, finalized_rejection.text
        after_rejection = client.get("/api/v1/knowledge/cards")
        assert after_rejection.status_code == 200, after_rejection.text
        assert after_rejection.json()["fingerprint"] == baseline_fingerprint

        retry_batch = _import_candidate_batch(client, retry_import_id)
        _, retry_batch = _review_and_promote_work_candidate(
            client,
            retry_import_id,
            retry_batch,
            card_id=card_id,
            promise="这个 staged 版本会被明确拒绝，但审计历史必须保留。",
            replace_active=True,
        )
        finalized_retry = _finalize_source_batch(
            client,
            retry_import_id,
            retry_receipt,
            retry_batch["candidate_set_hash"],
        )
        assert finalized_retry.status_code == 200, finalized_retry.text
        recovered = client.get("/api/v1/knowledge/cards")
        assert recovered.status_code == 200, recovered.text
        recovered_card = next(
            card for card in recovered.json()["cards"] if card["card_id"] == card_id
        )
        assert recovered_card["active_version"]["formalization_status"] == "verified"
        assert any(
            version["version_id"] == staged_version_id
            and version["formalization_status"] == "abandoned"
            for version in recovered_card["versions"]
        )
        assert recovered.json()["fingerprint"] != baseline_fingerprint


def test_project_knowledge_scope_isolated_in_context_and_survives_restart(
    tmp_path: Path,
) -> None:
    """A source batch and its formal cards must stay inside one project scope."""

    projects_root = tmp_path / "projects"
    project_a = _write_project(projects_root / "novel-a")
    project_b = _write_project(projects_root / "novel-b")
    settings = _settings(tmp_path, trusted_project_roots=(projects_root,))
    store = SqliteStore(settings.database_path)
    store.initialize()
    source_import_id = "source-import-" + "a" * 32
    receipt = _seed_source_import(
        store,
        settings.database_path,
        import_id=source_import_id,
        card_id="work-project-only",
    )

    with TestClient(create_app(settings)) as client:
        inspected_a = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_a)},
        )
        inspected_b = client.post(
            "/api/v1/projects/inspect",
            json={"root_path": str(project_b)},
        )
        assert inspected_a.status_code == 200, inspected_a.text
        assert inspected_b.status_code == 200, inspected_b.text
        project_a_payload = inspected_a.json()["project"]
        project_b_payload = inspected_b.json()["project"]
        project_a_id = project_a_payload["project_id"]
        project_b_id = project_b_payload["project_id"]
        scope_path = f"/api/v1/projects/{project_a_id}/knowledge"

        scope = client.get(f"{scope_path}/scope")
        assert scope.status_code == 200, scope.text
        assert scope.json()["scope_type"] == "project"
        assert scope.json()["project_binding_id"] == project_a_id

        # Read-only status checks from the source panel must not claim the
        # application scope before the author opens this batch in a project.
        unbound_status = client.get(
            f"/api/v1/knowledge/source-imports/{source_import_id}/formalization"
        )
        assert unbound_status.status_code == 200, unbound_status.text
        assert unbound_status.json()["status"] == "awaiting_review"
        with client.app.state.runtime.store.connection() as connection:
            assert connection.execute(
                "SELECT 1 FROM knowledge_source_scope WHERE source_import_id = ?",
                (source_import_id,),
            ).fetchone() is None

        imported = client.post(
            f"{scope_path}/source-imports/{source_import_id}/candidates"
        )
        assert imported.status_code == 201, imported.text
        pending = next(
            item
            for item in imported.json()["candidates"]
            if item["card_id"] == "work-project-only"
        )
        reviewed = client.put(
            f"{scope_path}/candidates/{pending['candidate_id']}",
            json={
                "expected_revision": pending["revision"],
                "expected_content_hash": pending["content_hash"],
                "card": _reviewed_work_card(
                    "work-project-only",
                    promise="项目 A 专属的阶段兑现规则，所有兑现都必须改变本项目角色的下一步选择空间。",
                ),
            },
        )
        assert reviewed.status_code == 200, reviewed.text
        promoted = client.post(
            f"{scope_path}/candidates/{pending['candidate_id']}/promote",
            json={
                "expected_revision": reviewed.json()["revision"],
                "expected_content_hash": reviewed.json()["content_hash"],
                "replace_active": False,
            },
        )
        assert promoted.status_code == 200, promoted.text
        assert (
            promoted.json()["active_version"]["lineage"]["schema"]
            == "zhimeng.knowledge-version-lineage-receipt.v2"
        )
        assert (
            promoted.json()["active_version"]["lineage"]["scope"]["project_binding_id"]
            == project_a_id
        )

        for support_candidate in imported.json()["candidates"]:
            if support_candidate["card_id"] == pending["card_id"]:
                continue
            rejected_support = client.post(
                f"{scope_path}/candidates/{support_candidate['candidate_id']}/reject",
                json={
                    "expected_revision": support_candidate["revision"],
                    "expected_content_hash": support_candidate["content_hash"],
                },
            )
            assert rejected_support.status_code == 200, rejected_support.text

        project_b_path = f"/api/v1/projects/{project_b_id}/knowledge"
        wrong_scope = client.post(
            f"{project_b_path}/source-imports/{source_import_id}/candidates"
        )
        assert wrong_scope.status_code == 409, wrong_scope.text
        assert client.get(f"{project_b_path}/cards").json()["cards"] == []
        assert client.get("/api/v1/knowledge/cards").json()["cards"] == []
        candidates = client.get(
            f"{scope_path}/candidates",
            params={"source_import_id": source_import_id},
        )
        assert candidates.status_code == 200, candidates.text
        finalized = client.post(
            f"{scope_path}/source-imports/{source_import_id}/formalization/finalize",
            json={
                "expected_source_receipt_hash": receipt.receipt_hash,
                "expected_draft_hash": receipt.cards_draft_hash,
                "expected_candidate_set_hash": candidates.json()["candidate_set_hash"],
            },
        )
        assert finalized.status_code == 200, finalized.text
        finalized_payload = finalized.json()
        assert finalized_payload["schema"] == "zhimeng.knowledge-formalization-receipt.v2"
        assert finalized_payload["scope"]["project_binding_id"] == project_a_id

        scoped_cards = client.get(f"{scope_path}/cards")
        assert scoped_cards.status_code == 200, scoped_cards.text
        assert scoped_cards.json()["inherited_card_ids"] == []
        assert [item["card_id"] for item in scoped_cards.json()["cards"]] == [
            "work-project-only"
        ]
        assert client.get(f"{project_b_path}/cards").json()["cards"] == []
        assert client.get("/api/v1/knowledge/cards").json()["cards"] == []
        project_card = scoped_cards.json()["cards"][0]
        project_selector = {
            "schema": "zhimeng.formal-knowledge-selector.v1",
            "source_import_id": source_import_id,
            "formalization_receipt_id": finalized_payload[
                "formalization_receipt_id"
            ],
            "formalization_receipt_hash": finalized_payload["receipt_hash"],
            "expected_knowledge_fingerprint": scoped_cards.json()["fingerprint"],
            "cards": [
                {
                    "card_id": project_card["card_id"],
                    "version_id": project_card["active_version_id"],
                    "content_hash": project_card["active_version"]["content_hash"],
                }
            ],
        }
        project_a_kb_binding = _publish_project_kb(client, project_a_payload)
        project_b_kb_binding = _publish_project_kb(client, project_b_payload)

        agent_store = AgentStore(client.app.state.runtime.store)
        sessions = {}
        for project_id, label in ((project_a_id, "scope-a"), (project_b_id, "scope-b")):
            sessions[project_id] = agent_store.create_session(
                project_id=project_id,
                title=label,
                executor=ExecutorIdentity(
                    executor_id="executor.scope-test",
                    kind=ExecutorKind.EMBEDDED,
                    implementation="scope-test",
                    version="1",
                    protocol_version="1",
                    instance_id=None,
                ),
                capabilities=CapabilityProfile(
                    protocol_version="1",
                    names=[CapabilityName.STREAMING],
                    extensions={},
                ),
            )

        context_a = client.post(
            f"/api/v1/agent/sessions/{sessions[project_a_id].session_id}/context-packs",
            headers={"Idempotency-Key": "project-scope-context-a-0001"},
            json={
                "objective": "验证项目 A 的专属阶段兑现规则。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_a_payload["revision"],
                "expected_project_fingerprint": project_a_payload["fingerprint"],
                **project_a_kb_binding,
            },
        )
        context_b = client.post(
            f"/api/v1/agent/sessions/{sessions[project_b_id].session_id}/context-packs",
            headers={"Idempotency-Key": "project-scope-context-b-0001"},
            json={
                "objective": "验证项目 B 不应读取项目 A 的规则。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_b_payload["revision"],
                "expected_project_fingerprint": project_b_payload["fingerprint"],
                **project_b_kb_binding,
            },
        )
        assert context_a.status_code == 201, context_a.text
        assert context_b.status_code == 201, context_b.text
        assert any(
            item["source_id"] == "knowledge-card:work-project-only"
            for item in context_a.json()["pack"]["included"]
        )
        assert all(
            item["source_id"] != "knowledge-card:work-project-only"
            for item in context_b.json()["pack"]["included"]
        )
        assert context_a.json()["pack"]["knowledge_fingerprint"] != context_b.json()["pack"][
            "knowledge_fingerprint"
        ]
        cross_project = client.post(
            f"/api/v1/agent/sessions/{sessions[project_b_id].session_id}/context-packs",
            headers={"Idempotency-Key": "project-scope-context-cross-selector"},
            json={
                "objective": "项目 B 不能显式复用项目 A 的知识批次。",
                "profile": "chapter",
                "active_chapter_id": "VOL001-CH001",
                "expected_project_revision": project_b_payload["revision"],
                "expected_project_fingerprint": project_b_payload["fingerprint"],
                **project_b_kb_binding,
                "formal_knowledge_selector": project_selector,
            },
        )
        assert cross_project.status_code == 409, cross_project.text
        assert cross_project.json()["error"]["code"] == "context_conflict"

    # A fresh Runtime process must reconstruct the scope and v2 receipt from SQLite.
    with TestClient(create_app(settings)) as client:
        inspected = client.get(f"/api/v1/projects/{project_a_id}/inspect")
        assert inspected.status_code == 200, inspected.text
        recovered_kb = client.get(
            f"/api/v1/projects/{project_a_id}/knowledge/kb-state"
        )
        assert recovered_kb.status_code == 200, recovered_kb.text
        assert recovered_kb.json()["status"] == "current"
        assert {
            "project_kb_state_id": recovered_kb.json()["manifest"]["state_id"],
            "project_kb_manifest_hash": recovered_kb.json()["manifest"][
                "manifest_hash"
            ],
        } == project_a_kb_binding
        recovered = client.get(f"/api/v1/projects/{project_a_id}/knowledge/cards")
        assert recovered.status_code == 200, recovered.text
        assert recovered.json()["inherited_card_ids"] == []
        assert recovered.json()["cards"][0]["card_id"] == "work-project-only"
        formalization = client.get(
            f"/api/v1/projects/{project_a_id}/knowledge/source-imports/{source_import_id}/formalization"
        )
        assert formalization.status_code == 200, formalization.text
        assert formalization.json()["receipt"]["schema"] == (
            "zhimeng.knowledge-formalization-receipt.v2"
        )

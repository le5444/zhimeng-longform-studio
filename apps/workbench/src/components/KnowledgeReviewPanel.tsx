import { useEffect, useMemo, useState } from "react";
import {
  AlertTriangle,
  Archive,
  BookCheck,
  CheckCircle2,
  CircleX,
  Database,
  FilterX,
  History,
  LoaderCircle,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldCheck,
  Sparkles,
  Trash2,
} from "lucide-react";
import {
  currentProjectKbBinding,
  type KnowledgeCandidate,
  type KnowledgeCard,
  type KnowledgeReviewIssue,
} from "../api/knowledge";
import type { FormalKnowledgeSelector } from "../api/contextPacks";
import { useKnowledgeReview } from "../hooks/useKnowledgeReview";
import { useProjectKbState } from "../hooks/useProjectKbState";

const ladderSteps = [
  ["internal_consistency", "内部一致"],
  ["predictive_validation", "预测验证"],
  ["blind_sample_validation", "盲样验证"],
  ["extreme_scenario_stress_test", "极端压力"],
] as const;

type LadderStep = (typeof ladderSteps)[number][0];

const projectKbReasonLabels: Record<string, string> = {
  knowledge_changed_during_read: "知识状态在读取期间发生变化",
  knowledge_fingerprint_changed: "知识指纹已变化",
  knowledge_projection_changed: "候选或版本已变化",
  knowledge_scope_missing: "项目知识范围不存在",
  knowledge_scope_revision_changed: "项目知识修订已变化",
  project_files_changed: "小说项目文件已变化",
  project_fingerprint_changed: "项目指纹已变化",
  project_revision_changed: "项目修订已变化",
  runtime_knowledge_unavailable: "Runtime 知识状态暂不可用",
};

function projectKbReasonLabel(reason: string): string {
  return projectKbReasonLabels[reason] ?? reason;
}

type ValidatorExecutionRuleKind = "forbid_terms" | "require_any_term";

interface ValidatorExecutionRuleForm {
  ruleId: string;
  kind: ValidatorExecutionRuleKind;
  terms: string;
  caseSensitive: boolean;
  failureSignature: string;
  message: string;
}

const validatorLayers = [
  "L0_INPUT",
  "L1_PROJECT_TRUTH",
  "L2_CONTROL_CARD",
  "L3_DISTILLATION_RETRIEVAL",
  "L4_DRAFT_EXECUTION",
  "L5_DEAI_STYLE",
  "L6_REVIEW_ACCEPTANCE",
  "L7_WRITEBACK",
] as const;

interface ReviewForm {
  cardId: string;
  title: string;
  summary: string;
  readerPromise: string;
  tags: string;
  keywords: string;
  applicableTasks: string;
  sourceScope: string;
  evidenceQuality: "weak" | "medium" | "strong";
  evidenceNotes: string;
  premisePattern: string;
  coreReaderPromise: string;
  openingContract: string;
  progressionMap: string;
  recurringLoops: string;
  transferLimits: string;
  taxonomyPlatform: string;
  taxonomyAudience: string;
  taxonomyGenre: string;
  taxonomySubgenre: string;
  taxonomySceneFunction: string;
  taxonomyEmotion: string;
  taxonomyCraft: string;
  taxonomyStyle: string;
  taxonomyPov: string;
  taxonomyContentBoundary: string;
  taxonomyProblemSymptom: string;
  povDistance: string;
  sentenceRhythm: string;
  dictionPool: string;
  dialogueBehavior: string;
  descriptionBehavior: string;
  emotionHandling: string;
  humanWebProse: string;
  sentenceVariety: string;
  sceneAnchorRules: string;
  dialoguePressureRules: string;
  antiAiFailureSignatures: string;
  readabilityAcceptanceTests: string;
  doNotCopy: string;
  validatorName: string;
  validatorAppliesTo: string;
  validatorLayerId: string;
  validatorChecks: string;
  validatorRejectConditions: string;
  validatorExecutionRules: ValidatorExecutionRuleForm[];
  validatorRewriteRoutes: string;
  nlclLayers: string;
  mechanicName: string;
  mechanicUseWhen: string;
  mechanicRule: string;
  mechanicFailure: string;
  authorIntent: string;
  observedEffect: string;
  effectEvidence: string;
  effectFailure: string;
  misreadRisk: string;
  turnsBad: string;
  validationTests: string;
  ladderEvidence: Record<LadderStep, string>;
  ladderPassed: Record<LadderStep, boolean>;
  evidenceScore: string;
  specificityScore: string;
  generativityScore: string;
  retrievalScore: string;
  safetyRiskScore: string;
  routeRecommendedProfiles: string;
  routeTerms: string;
  negativeRoutes: string;
  safetyCopyrightBoundary: string;
  safetyDoNotReuse: string;
}

function record(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function strings(value: unknown): string[] {
  return Array.isArray(value) ? value.map(String) : [];
}

function firstMechanic(card: Record<string, unknown>): Record<string, unknown> {
  const mechanics = card.mechanics;
  return Array.isArray(mechanics) ? record(mechanics[0]) : {};
}

function evidenceLines(value: unknown): string {
  return strings(value).join("\n");
}

function validatorExecutionRules(value: unknown): ValidatorExecutionRuleForm[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((item) => {
      const rule = record(item);
      const kind: ValidatorExecutionRuleKind =
        rule.kind === "require_any_term" ? "require_any_term" : "forbid_terms";
      return {
        ruleId: String(rule.rule_id ?? ""),
        kind,
        terms: strings(rule.terms).join("\n"),
        caseSensitive: rule.case_sensitive === true,
        failureSignature: String(rule.failure_signature ?? ""),
        message: String(rule.message ?? ""),
      };
    })
    .sort((left, right) =>
      left.ruleId < right.ruleId ? -1 : left.ruleId > right.ruleId ? 1 : 0,
    );
}

function formFromCandidate(candidate: KnowledgeCandidate): ReviewForm {
  const card = candidate.card;
  const source = record(card.source);
  const taxonomy = record(card.taxonomy);
  const payload = record(card.type_payload);
  const mechanic = firstMechanic(card);
  const effect = record(card.reader_effect);
  const ladder = record(card.validation_ladder);
  const scores = record(card.scores);
  const routeHints = record(card.nlcl_route_hints);
  const safety = record(card.safety);
  const ladderEvidence = {} as Record<LadderStep, string>;
  const ladderPassed = {} as Record<LadderStep, boolean>;
  for (const [step] of ladderSteps) {
    const entry = record(ladder[step]);
    ladderEvidence[step] = evidenceLines(entry.evidence);
    ladderPassed[step] = entry.status === "pass";
  }
  return {
    cardId: String(card.card_id ?? candidate.card_id ?? ""),
    title: String(card.title ?? ""),
    summary: String(card.summary ?? ""),
    readerPromise: String(card.reader_promise ?? ""),
    tags: strings(card.tags).join("\n"),
    keywords: strings(card.keywords).join("\n"),
    applicableTasks: strings(card.applicable_tasks).join("\n"),
    sourceScope: String(source.scope ?? ""),
    evidenceQuality: ["medium", "strong"].includes(
      String(source.evidence_quality),
    )
      ? (String(source.evidence_quality) as "medium" | "strong")
      : "weak",
    evidenceNotes: String(source.notes ?? ""),
    premisePattern: String(payload.premise_pattern ?? ""),
    coreReaderPromise: String(payload.core_reader_promise ?? ""),
    openingContract: String(payload.opening_contract ?? ""),
    progressionMap: strings(payload.progression_map).join("\n"),
    recurringLoops: strings(payload.recurring_scene_loops).join("\n"),
    transferLimits: strings(payload.transfer_limits).join("\n"),
    taxonomyPlatform: strings(taxonomy.platform).join("\n"),
    taxonomyAudience: strings(taxonomy.audience).join("\n"),
    taxonomyGenre: strings(taxonomy.genre).join("\n"),
    taxonomySubgenre: strings(taxonomy.subgenre).join("\n"),
    taxonomySceneFunction: strings(taxonomy.scene_function).join("\n"),
    taxonomyEmotion: strings(taxonomy.emotion).join("\n"),
    taxonomyCraft: strings(taxonomy.craft).join("\n"),
    taxonomyStyle: strings(taxonomy.style).join("\n"),
    taxonomyPov: strings(taxonomy.pov).join("\n"),
    taxonomyContentBoundary: strings(taxonomy.content_boundary).join("\n"),
    taxonomyProblemSymptom: strings(taxonomy.problem_symptom).join("\n"),
    povDistance: String(payload.pov_distance ?? ""),
    sentenceRhythm: String(payload.sentence_rhythm ?? ""),
    dictionPool: strings(payload.diction_pool).join("\n"),
    dialogueBehavior: String(payload.dialogue_behavior ?? ""),
    descriptionBehavior: String(payload.description_behavior ?? ""),
    emotionHandling: String(payload.emotion_handling ?? ""),
    humanWebProse: String(payload.human_web_prose ?? ""),
    sentenceVariety: String(payload.sentence_variety ?? ""),
    sceneAnchorRules: strings(payload.scene_anchor_rules).join("\n"),
    dialoguePressureRules: strings(payload.dialogue_pressure_rules).join("\n"),
    antiAiFailureSignatures: strings(payload.anti_ai_failure_signatures).join(
      "\n",
    ),
    readabilityAcceptanceTests: strings(
      payload.readability_acceptance_tests,
    ).join("\n"),
    doNotCopy: strings(payload.do_not_copy).join("\n"),
    validatorName: String(payload.validator_name ?? ""),
    validatorAppliesTo: strings(payload.applies_to).join("\n"),
    validatorLayerId: String(payload.layer_id ?? "L6_REVIEW_ACCEPTANCE"),
    validatorChecks: strings(payload.checks).join("\n"),
    validatorRejectConditions: strings(payload.reject_conditions).join("\n"),
    validatorExecutionRules: validatorExecutionRules(payload.execution_rules),
    validatorRewriteRoutes: strings(payload.rewrite_routes).join("\n"),
    nlclLayers: strings(routeHints.nlcl_layers).join("\n"),
    mechanicName: String(mechanic.name ?? ""),
    mechanicUseWhen: String(mechanic.use_when ?? ""),
    mechanicRule: String(mechanic.rule ?? ""),
    mechanicFailure: String(mechanic.failure_mode ?? ""),
    authorIntent: String(effect.author_intent ?? ""),
    observedEffect: String(effect.observed_reader_effect ?? ""),
    effectEvidence: evidenceLines(effect.effect_evidence),
    effectFailure: String(effect.effect_failure_boundary ?? ""),
    misreadRisk: String(effect.misread_risk ?? ""),
    turnsBad: String(effect.when_it_turns_bad ?? ""),
    validationTests: strings(card.validation_tests).join("\n"),
    ladderEvidence,
    ladderPassed,
    evidenceScore: String(scores.evidence ?? ""),
    specificityScore: String(scores.specificity ?? ""),
    generativityScore: String(scores.generativity ?? ""),
    retrievalScore: String(scores.retrieval ?? ""),
    safetyRiskScore: String(scores.safety_risk ?? ""),
    routeRecommendedProfiles: strings(routeHints.recommended_profiles).join(
      "\n",
    ),
    routeTerms: strings(routeHints.route_terms).join("\n"),
    negativeRoutes: strings(routeHints.negative_routes).join("\n"),
    safetyCopyrightBoundary: String(safety.copyright_boundary ?? ""),
    safetyDoNotReuse: strings(safety.do_not_reuse).join("\n"),
  };
}

function lines(value: string): string[] {
  return value
    .split("\n")
    .map((item) => item.trim())
    .filter(Boolean);
}

function textFromForm(value: string): string {
  return value.trim();
}

function listFromForm(value: string, minItem = 1): string[] {
  return lines(value).filter((item) => item.length >= minItem);
}

function optionalScore(value: string): number | undefined {
  const trimmed = value.trim();
  if (!trimmed) return undefined;
  const number = Number(trimmed);
  return Number.isFinite(number) ? number : undefined;
}

function hasText(value: unknown, minimum: number): boolean {
  return String(value ?? "").trim().length >= minimum;
}

function hasLines(value: string, minimum: number, minItem = 1): boolean {
  return listFromForm(value, minItem).length >= minimum;
}

function cardIdProblem(value: string): string | null {
  const trimmed = value.trim();
  if (!trimmed) {
    return "card_id 不能为空；请保留 Provider 给出的稳定 ID，或在这里显式改成合法 ID。";
  }
  if (trimmed.length > 150) return "card_id 不能超过 150 个字符。";
  if (!/^[A-Za-z0-9_.\-\u4e00-\u9fff]+$/.test(trimmed)) {
    return "card_id 只能包含字母、数字、中文、点、下划线和短横线，不能静默清洗。";
  }
  return null;
}

function routeIdentityComplete(form: ReviewForm): boolean {
  return (
    hasLines(form.taxonomyPlatform, 1, 1) ||
    hasLines(form.taxonomyAudience, 1, 1) ||
    hasLines(form.taxonomyGenre, 1, 1) ||
    hasLines(form.taxonomySubgenre, 1, 1)
  );
}

function scorePasses(
  value: string,
  minimum?: number,
  maximum?: number,
): boolean {
  const number = optionalScore(value);
  if (number === undefined || number < 0 || number > 1) return false;
  if (minimum !== undefined && number < minimum) return false;
  if (maximum !== undefined && number > maximum) return false;
  return true;
}

function validatorRuleComplete(rule: ValidatorExecutionRuleForm): boolean {
  return (
    /^[a-z][a-z0-9_]{2,127}$/.test(rule.ruleId.trim()) &&
    hasLines(rule.terms, 1, 2) &&
    /^[a-z][a-z0-9_]{2,127}$/.test(rule.failureSignature.trim()) &&
    hasText(rule.message, 8)
  );
}

function reviewCompletionProblems(
  candidate: KnowledgeCandidate,
  form: ReviewForm,
): string[] {
  const problems: string[] = [];

  const require = (condition: boolean, message: string) => {
    if (!condition) problems.push(message);
  };

  const cardIdIssue = cardIdProblem(form.cardId);
  require(cardIdIssue === null, cardIdIssue ?? "card_id 不合法。");
  require(hasText(form.title, 8), "标题至少 8 个字符。");
  require(hasText(form.summary, 40), "摘要至少 40 个字符。");
  require(hasText(form.readerPromise, 20), "读者承诺至少 20 个字符。");
  require(form.evidenceQuality === "medium" ||
    form.evidenceQuality === "strong", "证据强度必须为中或强。");
  require(hasText(form.evidenceNotes, 12), "证据说明至少 12 个字符。");
  require(hasText(form.sourceScope, 12), "来源范围至少 12 个字符。");
  require(hasLines(
    form.tags,
    candidate.card_type === "work" ? 3 : 6,
    2,
  ), "标签数量不足。");
  require(hasLines(form.keywords, 2, 2), "关键词至少 2 条。");
  require(hasLines(form.applicableTasks, 1, 2), "适用任务至少 1 条。");

  if (candidate.card_type === "work") {
    for (const [key, label] of [
      ["genre", "题材分类"],
      ["scene_function", "场景功能"],
      ["emotion", "情绪分类"],
      ["craft", "技法分类"],
    ] as const) {
      const formKey = {
        genre: "taxonomyGenre",
        scene_function: "taxonomySceneFunction",
        emotion: "taxonomyEmotion",
        craft: "taxonomyCraft",
      }[key] as keyof ReviewForm;
      require(typeof form[formKey] === "string" &&
        hasLines(form[formKey] as string, 1, 1), `作品机制缺少 ${label}。`);
    }
    for (const [value, label] of [
      [form.premisePattern, "前提模式"],
      [form.coreReaderPromise, "核心承诺"],
      [form.openingContract, "开篇合同"],
    ] as const) {
      require(hasText(value, 12), `${label}至少 12 个字符。`);
    }
    require(hasLines(form.progressionMap, 1, 8), "成长路线至少 1 条。");
    require(hasLines(form.recurringLoops, 1, 8), "循环场景至少 1 条。");
    require(hasLines(form.transferLimits, 1, 8), "迁移边界至少 1 条。");
  } else if (candidate.card_type === "voice") {
    require(routeIdentityComplete(
      form,
    ), "声音规则需要保留平台 / 受众 / 题材路由之一。");
    require(hasLines(form.taxonomyStyle, 1, 1), "风格分类至少 1 条。");
    require(hasLines(form.taxonomyPov, 1, 1), "视角分类至少 1 条。");
    require(hasLines(
      form.taxonomyContentBoundary,
      1,
      1,
    ), "内容边界至少 1 条。");
    for (const [value, label] of [
      [form.povDistance, "视角距离"],
      [form.sentenceRhythm, "句式节奏"],
      [form.dialogueBehavior, "对话行为"],
      [form.descriptionBehavior, "描写行为"],
      [form.emotionHandling, "情绪处理"],
      [form.humanWebProse, "自然网文表达"],
      [form.sentenceVariety, "句式变化"],
    ] as const) {
      require(hasText(value, 12), `${label}至少 12 个字符。`);
    }
    require(hasLines(form.dictionPool, 1, 6), "用词池至少 1 条。");
    require(hasLines(form.sceneAnchorRules, 1, 6), "场景锚点规则至少 1 条。");
    require(hasLines(
      form.dialoguePressureRules,
      1,
      6,
    ), "对话压力规则至少 1 条。");
    require(hasLines(
      form.antiAiFailureSignatures,
      3,
      6,
    ), "AI 痕迹失败特征至少 3 条。");
    require(hasLines(
      form.readabilityAcceptanceTests,
      1,
      6,
    ), "可读性验收测试至少 1 条。");
    require(hasLines(form.doNotCopy, 3, 6), "禁止复制内容至少 3 条。");
    const nlclLayers = new Set(listFromForm(form.nlclLayers, 1));
    require(nlclLayers.has("style") &&
      nlclLayers.has("deai"), "声音规则需要 style 和 deai 路由层。");
  } else if (candidate.card_type === "validator") {
    require(routeIdentityComplete(
      form,
    ), "正文校验器需要保留平台 / 受众 / 题材路由之一。");
    require(hasLines(form.taxonomyProblemSymptom, 1, 1), "问题症状至少 1 条。");
    require(hasLines(
      form.taxonomyContentBoundary,
      1,
      1,
    ), "内容边界至少 1 条。");
    require(/^[a-z][a-z0-9_]{2,127}$/.test(
      form.validatorName.trim(),
    ), "校验器名称必须是 snake_case。");
    require(hasLines(form.validatorAppliesTo, 1, 8), "适用对象至少 1 条。");
    require(validatorLayers.includes(
      form.validatorLayerId as (typeof validatorLayers)[number],
    ), "执行层必须选择已知 NLCL 层。");
    require(hasLines(form.validatorChecks, 2, 8), "检查项至少 2 条。");
    require(hasLines(
      form.validatorRejectConditions,
      2,
      8,
    ), "拒收条件至少 2 条。");
    require(form.validatorExecutionRules.length >
      0, "至少需要 1 条正文执行规则。");
    require(form.validatorExecutionRules.length <=
      16, "正文执行规则不能超过 16 条。");
    for (const rule of form.validatorExecutionRules) {
      require(validatorRuleComplete(
        rule,
      ), `执行规则 ${rule.ruleId || "未命名"} 未填写完整。`);
    }
    const nlclLayers = new Set(listFromForm(form.nlclLayers, 1));
    require(nlclLayers.has("review"), "正文校验器需要 review 路由层。");
    require(hasLines(form.validatorRewriteRoutes, 1, 8), "重写路由至少 1 条。");
  }

  for (const [value, label] of [
    [form.mechanicName, "机制名"],
    [form.mechanicUseWhen, "适用时机"],
    [form.mechanicRule, "规则"],
    [form.mechanicFailure, "失败模式"],
  ] as const) {
    require(hasText(value, 8), `${label}至少 8 个字符。`);
  }
  for (const [value, label] of [
    [form.authorIntent, "作者意图"],
    [form.observedEffect, "观察效果"],
    [form.effectFailure, "失效边界"],
    [form.misreadRisk, "误读风险"],
    [form.turnsBad, "转坏节点"],
  ] as const) {
    require(hasText(value, 12), `${label}至少 12 个字符。`);
  }
  require(hasLines(form.effectEvidence, 1, 12), "效果证据至少 1 条。");
  require(hasLines(form.validationTests, 2, 12), "验收测试至少 2 条。");
  for (const [step, label] of ladderSteps) {
    require(form.ladderPassed[step], `${label}验证未勾选通过。`);
    require(hasLines(
      form.ladderEvidence[step],
      1,
      8,
    ), `${label}验证缺少证据。`);
  }
  require(scorePasses(form.evidenceScore, 0.6), "证据分不足 0.6。");
  require(scorePasses(form.specificityScore, 0.6), "具体分不足 0.6。");
  require(scorePasses(form.generativityScore, 0.6), "生成分不足 0.6。");
  require(scorePasses(form.retrievalScore, 0.5), "检索分不足 0.5。");
  require(scorePasses(
    form.safetyRiskScore,
    undefined,
    0.45,
  ), "风险分必须不高于 0.45。");
  require(hasText(
    form.safetyCopyrightBoundary,
    12,
  ), "版权边界至少 12 个字符。");
  require(hasLines(form.safetyDoNotReuse, 3, 1), "禁止复用边界至少 3 条。");
  require(hasLines(
    form.routeRecommendedProfiles,
    1,
    1,
  ), "推荐 profiles 至少 1 条。");
  require(hasLines(form.routeTerms, 2, 1), "路由词至少 2 条。");
  require(hasLines(form.negativeRoutes, 1, 1), "负路由至少 1 条。");
  return problems;
}

function issueText(issue: KnowledgeReviewIssue): string {
  const labels: Record<string, string> = {
    invalid_card_identity: "卡片标识不符合稳定 ID 规则",
    card_type_not_supported: "当前只支持 work、voice 和 validator 卡的严格晋升",
    draft_flag_present: "仍标记为蒸馏草案",
    human_review_flag_present: "尚未确认人工复核",
    missing_source: "缺少来源与证据边界",
    evidence_quality_too_weak: "证据强度未达到正式卡要求",
    missing_taxonomy: "缺少检索分类",
    missing_type_payload: "缺少该卡片类型的专属结构",
    missing_mechanics: "缺少可执行机制",
    invalid_mechanic: "机制结构无效",
    missing_validation_ladder: "缺少四级验证记录",
    missing_validation_step: "验证步骤不完整",
    validation_not_passed: "验证步骤尚未通过",
    missing_reader_effect: "缺少读者效果证据",
    missing_scores: "缺少证据与安全评分",
    invalid_score: "评分不是 0 到 1 的数字",
    score_gate_failed: "评分未达到晋升门槛",
    missing_safety: "缺少防污染边界",
    missing_route_hints: "缺少 NLCL 路由提示",
    missing_route_identity: "缺少平台、受众或题材路由标识",
    invalid_feedback_layer: "反馈层与卡片的 NLCL 执行层不一致",
    missing_route_layer: "缺少该卡片必需的 NLCL 路由层",
    invalid_validator_layer: "校验器执行层不在受管 NLCL 层内",
    missing_validator_execution_rules: "缺少可执行的正文校验规则",
    validator_execution_rules_limit_exceeded: "正文校验规则不能超过 16 条",
    invalid_validator_execution_rule: "正文校验规则结构无效",
    invalid_validator_execution_rule_order:
      "正文校验规则必须按规则 ID 排序且不能重复",
    duplicate_validator_failure_signature: "正文校验规则的失败签名不能重复",
    placeholder_remains: "仍含待补或未验证占位内容",
    field_too_shallow: "内容过短或缺少具体证据",
    missing_required_field: "缺少正式卡必填字段",
  };
  return labels[issue.code] ?? issue.message;
}

const candidateTypeLabels: Record<string, { title: string; subtitle: string }> =
  {
    work: { title: "作品机制", subtitle: "读者承诺、节奏与爽点结构" },
    voice: { title: "声音规则", subtitle: "叙述距离、句式、对白与去 AI 味" },
    validator: {
      title: "正文校验器",
      subtitle: "拒收条件、修复路线与执行规则",
    },
  };

function candidateTypeLabel(candidate: KnowledgeCandidate) {
  return (
    candidateTypeLabels[candidate.card_type] ?? {
      title: candidate.card_type,
      subtitle: "暂存候选，等待作者判断",
    }
  );
}

function candidateStatusText(
  candidate: KnowledgeCandidate,
  batchFinalized: boolean,
): string {
  if (candidate.status === "promoted")
    return batchFinalized ? "已正式化" : "待整批正式化";
  if (candidate.status === "rejected") return "已拒绝";
  if (candidate.status === "deferred") return "已延期";
  return candidate.issues.length
    ? `${candidate.issues.length} 项待修`
    : "可接受";
}

function candidateSummary(candidate: KnowledgeCandidate): string {
  const summary = String(candidate.card.summary ?? "").trim();
  if (summary) return summary;
  const readerPromise = String(candidate.card.reader_promise ?? "").trim();
  if (readerPromise) return readerPromise;
  return "候选已持久化，等待作者复核。";
}

function sourceAnchorText(candidate: KnowledgeCandidate): string {
  const source = record(candidate.card.source);
  const notes = String(source.notes ?? "").trim();
  const quality = String(source.evidence_quality ?? "").trim();
  const anchor = `来源片段 #${candidate.source_card_index + 1}`;
  if (quality && notes) return `${anchor} · ${quality} · ${notes}`;
  if (quality) return `${anchor} · ${quality}`;
  if (notes) return `${anchor} · ${notes}`;
  return anchor;
}

function workflowStepState(
  index: number,
  activeStep: number,
): "done" | "active" | "next" {
  if (index < activeStep) return "done";
  if (index === activeStep) return "active";
  return "next";
}

function KnowledgeWorkflowSteps({
  sourceImportId,
  sourceVerified,
  candidatesReady,
  reviewDone,
  batchFinalized,
}: {
  sourceImportId: string | null;
  sourceVerified: boolean;
  candidatesReady: boolean;
  reviewDone: boolean;
  batchFinalized: boolean;
}) {
  const activeStep = batchFinalized
    ? 4
    : reviewDone
      ? 3
      : candidatesReady
        ? 2
        : sourceVerified || sourceImportId
          ? 1
          : 0;
  const steps = [
    ["来源", sourceImportId ? "已选导入批次" : "等待来源"],
    ["深蒸馏", candidatesReady ? "三类候选已生成" : "准备或运行 Runtime"],
    ["候选审阅", reviewDone ? "候选已处理完" : "编辑 / 接受 / 拒绝"],
    ["正式知识", batchFinalized ? "三类知识已正式化" : "等待 finalize"],
    ["写作上下文", batchFinalized ? "可用于后续 Context" : "后续阶段入口"],
  ] as const;
  return (
    <ol className="knowledge-workflow" aria-label="资料到写作上下文流程">
      {steps.map(([title, subtitle], index) => {
        const state = workflowStepState(index, activeStep);
        return (
          <li key={title} className={`workflow-step workflow-${state}`}>
            <span>
              {state === "done" ? <CheckCircle2 size={13} /> : index + 1}
            </span>
            <div>
              <strong>{title}</strong>
              <small>{subtitle}</small>
            </div>
          </li>
        );
      })}
    </ol>
  );
}

function KnowledgeCandidateBoard({
  candidates,
  selectedId,
  batchFinalized,
  onSelect,
}: {
  candidates: KnowledgeCandidate[];
  selectedId: string | null;
  batchFinalized: boolean;
  onSelect: (candidateId: string) => void;
}) {
  const preferred = ["work", "voice", "validator"];
  const ordered = [...candidates].sort((left, right) => {
    const leftOrder = preferred.indexOf(left.card_type);
    const rightOrder = preferred.indexOf(right.card_type);
    return (
      (leftOrder === -1 ? 99 : leftOrder) -
        (rightOrder === -1 ? 99 : rightOrder) ||
      left.source_card_index - right.source_card_index
    );
  });
  return (
    <section className="knowledge-candidate-board" aria-label="三类知识候选">
      <div className="candidate-board-heading">
        <div>
          <strong>候选审阅</strong>
          <span>work / voice / validator 三类候选，先编辑，再接受或拒绝。</span>
        </div>
        <small>{ordered.length} 张候选</small>
      </div>
      <div
        className="candidate-review-cards"
        role="tablist"
        aria-label="知识候选"
      >
        {ordered.map((candidate) => {
          const label = candidateTypeLabel(candidate);
          const selected = selectedId === candidate.candidate_id;
          return (
            <button
              type="button"
              role="tab"
              aria-selected={selected}
              className={`candidate-review-card status-${candidate.status}${selected ? " is-active" : ""}`}
              key={candidate.candidate_id}
              onClick={() => onSelect(candidate.candidate_id)}
            >
              <span className="candidate-card-kicker">
                {candidate.card_type}
              </span>
              <strong>{label.title}</strong>
              <small>{label.subtitle}</small>
              <p>{candidateSummary(candidate)}</p>
              <em>{sourceAnchorText(candidate)}</em>
              <b>{candidateStatusText(candidate, batchFinalized)}</b>
            </button>
          );
        })}
      </div>
    </section>
  );
}

function reviewedCard(
  candidate: KnowledgeCandidate,
  form: ReviewForm,
  reviewConfirmed: boolean,
): Record<string, unknown> {
  const next = JSON.parse(JSON.stringify(candidate.card)) as Record<
    string,
    unknown
  >;
  const source = record(next.source);
  const taxonomy = record(next.taxonomy);
  const payload = record(next.type_payload);
  const effect = record(next.reader_effect);
  const scores = record(next.scores);
  const ladder = record(next.validation_ladder);
  const routeHints = record(next.nlcl_route_hints);
  const completed =
    reviewConfirmed && reviewCompletionProblems(candidate, form).length === 0;

  next.title = textFromForm(form.title);
  next.card_id = textFromForm(form.cardId);
  next.card_type = candidate.card_type;
  next.summary = textFromForm(form.summary);
  next.reader_promise = textFromForm(form.readerPromise);
  next.draft_candidate = !completed;
  next.requires_human_distillation = !completed;
  if ("requires_author_review" in next)
    next.requires_author_review = !completed;
  next.tags = listFromForm(form.tags, 1);
  next.keywords = listFromForm(form.keywords, 1);
  next.applicable_tasks = listFromForm(form.applicableTasks, 1);

  source.scope = textFromForm(form.sourceScope);
  source.evidence_quality = form.evidenceQuality;
  source.notes = textFromForm(form.evidenceNotes);
  next.source = source;

  if (candidate.card_type === "work") {
    taxonomy.genre = listFromForm(form.taxonomyGenre, 1);
    taxonomy.scene_function = listFromForm(form.taxonomySceneFunction, 1);
    taxonomy.emotion = listFromForm(form.taxonomyEmotion, 1);
    taxonomy.craft = listFromForm(form.taxonomyCraft, 1);
    payload.premise_pattern = textFromForm(form.premisePattern);
    payload.core_reader_promise = textFromForm(form.coreReaderPromise);
    payload.opening_contract = textFromForm(form.openingContract);
    payload.progression_map = listFromForm(form.progressionMap, 1);
    payload.recurring_scene_loops = listFromForm(form.recurringLoops, 1);
    payload.transfer_limits = listFromForm(form.transferLimits, 1);
  } else if (candidate.card_type === "voice") {
    taxonomy.platform = listFromForm(form.taxonomyPlatform, 1);
    taxonomy.audience = listFromForm(form.taxonomyAudience, 1);
    taxonomy.genre = listFromForm(form.taxonomyGenre, 1);
    taxonomy.subgenre = listFromForm(form.taxonomySubgenre, 1);
    taxonomy.style = listFromForm(form.taxonomyStyle, 1);
    taxonomy.pov = listFromForm(form.taxonomyPov, 1);
    taxonomy.content_boundary = listFromForm(form.taxonomyContentBoundary, 1);
    payload.pov_distance = textFromForm(form.povDistance);
    payload.sentence_rhythm = textFromForm(form.sentenceRhythm);
    payload.diction_pool = listFromForm(form.dictionPool, 1);
    payload.dialogue_behavior = textFromForm(form.dialogueBehavior);
    payload.description_behavior = textFromForm(form.descriptionBehavior);
    payload.emotion_handling = textFromForm(form.emotionHandling);
    payload.human_web_prose = textFromForm(form.humanWebProse);
    payload.sentence_variety = textFromForm(form.sentenceVariety);
    payload.scene_anchor_rules = listFromForm(form.sceneAnchorRules, 1);
    payload.dialogue_pressure_rules = listFromForm(
      form.dialoguePressureRules,
      1,
    );
    payload.anti_ai_failure_signatures = listFromForm(
      form.antiAiFailureSignatures,
      1,
    );
    payload.readability_acceptance_tests = listFromForm(
      form.readabilityAcceptanceTests,
      1,
    );
    payload.do_not_copy = listFromForm(form.doNotCopy, 1);
    routeHints.nlcl_layers = listFromForm(form.nlclLayers, 1);
    routeHints.feedback_default_layer = "L5_DEAI_STYLE";
  } else if (candidate.card_type === "validator") {
    taxonomy.platform = listFromForm(form.taxonomyPlatform, 1);
    taxonomy.audience = listFromForm(form.taxonomyAudience, 1);
    taxonomy.genre = listFromForm(form.taxonomyGenre, 1);
    taxonomy.subgenre = listFromForm(form.taxonomySubgenre, 1);
    taxonomy.problem_symptom = listFromForm(form.taxonomyProblemSymptom, 1);
    taxonomy.content_boundary = listFromForm(form.taxonomyContentBoundary, 1);
    payload.validator_name = textFromForm(form.validatorName);
    payload.applies_to = listFromForm(form.validatorAppliesTo, 1);
    payload.layer_id = validatorLayers.includes(
      form.validatorLayerId as (typeof validatorLayers)[number],
    )
      ? form.validatorLayerId
      : form.validatorLayerId;
    payload.checks = listFromForm(form.validatorChecks, 1);
    payload.reject_conditions = listFromForm(form.validatorRejectConditions, 1);
    payload.execution_rules = form.validatorExecutionRules
      .map((rule) => ({
        rule_id: rule.ruleId.trim(),
        kind: rule.kind,
        terms: listFromForm(rule.terms, 1),
        case_sensitive: rule.caseSensitive,
        failure_signature: rule.failureSignature.trim(),
        message: textFromForm(rule.message),
      }))
      .sort((left, right) =>
        left.rule_id < right.rule_id
          ? -1
          : left.rule_id > right.rule_id
            ? 1
            : 0,
      );
    payload.rewrite_routes = listFromForm(form.validatorRewriteRoutes, 1);
    routeHints.nlcl_layers = listFromForm(form.nlclLayers, 1);
    routeHints.feedback_default_layer = String(payload.layer_id);
  }

  routeHints.recommended_profiles = listFromForm(
    form.routeRecommendedProfiles,
    1,
  );
  routeHints.route_terms = listFromForm(form.routeTerms, 1);
  routeHints.negative_routes = listFromForm(form.negativeRoutes, 1);
  next.taxonomy = taxonomy;
  next.type_payload = payload;
  next.nlcl_route_hints = routeHints;
  next.mechanics = [
    {
      name: textFromForm(form.mechanicName),
      use_when: textFromForm(form.mechanicUseWhen),
      rule: textFromForm(form.mechanicRule),
      failure_mode: textFromForm(form.mechanicFailure),
    },
  ];
  effect.author_intent = textFromForm(form.authorIntent);
  effect.observed_reader_effect = textFromForm(form.observedEffect);
  effect.effect_evidence = listFromForm(form.effectEvidence, 1);
  effect.effect_failure_boundary = textFromForm(form.effectFailure);
  effect.misread_risk = textFromForm(form.misreadRisk);
  effect.when_it_turns_bad = textFromForm(form.turnsBad);
  next.reader_effect = effect;
  next.validation_tests = listFromForm(form.validationTests, 1);
  for (const [step] of ladderSteps) {
    const previous = record(ladder[step]);
    const previousStatus = String(previous.status ?? "pending")
      .trim()
      .toLowerCase();
    ladder[step] = {
      ...previous,
      status: form.ladderPassed[step]
        ? "pass"
        : previousStatus && previousStatus !== "pass"
          ? previousStatus
          : "pending",
      evidence: listFromForm(form.ladderEvidence[step], 1),
    };
  }
  next.validation_ladder = ladder;
  const scoreInputs = {
    evidence: optionalScore(form.evidenceScore),
    specificity: optionalScore(form.specificityScore),
    generativity: optionalScore(form.generativityScore),
    retrieval: optionalScore(form.retrievalScore),
    safety_risk: optionalScore(form.safetyRiskScore),
  };
  for (const [key, value] of Object.entries(scoreInputs)) {
    if (value === undefined) {
      delete scores[key];
    } else {
      scores[key] = value;
    }
  }
  next.scores = scores;
  const currentSafety = record(next.safety);
  currentSafety.copyright_boundary = textFromForm(form.safetyCopyrightBoundary);
  currentSafety.do_not_reuse = listFromForm(form.safetyDoNotReuse, 1);
  next.safety = currentSafety;
  return next;
}

function Field({
  label,
  value,
  multiline = false,
  onChange,
}: {
  label: string;
  value: string;
  multiline?: boolean;
  onChange: (value: string) => void;
}) {
  return (
    <label className="knowledge-field">
      <span>{label}</span>
      {multiline ? (
        <textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      ) : (
        <input
          value={value}
          onChange={(event) => onChange(event.target.value)}
        />
      )}
    </label>
  );
}

type ChangeReviewForm = <K extends keyof ReviewForm>(
  key: K,
  value: ReviewForm[K],
) => void;

function WorkFields({
  form,
  change,
}: {
  form: ReviewForm;
  change: ChangeReviewForm;
}) {
  return (
    <details className="knowledge-form-section" open>
      <summary>作品机制</summary>
      <Field
        label="题材分类"
        value={form.taxonomyGenre}
        multiline
        onChange={(value) => change("taxonomyGenre", value)}
      />
      <Field
        label="场景功能"
        value={form.taxonomySceneFunction}
        multiline
        onChange={(value) => change("taxonomySceneFunction", value)}
      />
      <Field
        label="情绪分类"
        value={form.taxonomyEmotion}
        multiline
        onChange={(value) => change("taxonomyEmotion", value)}
      />
      <Field
        label="技法分类"
        value={form.taxonomyCraft}
        multiline
        onChange={(value) => change("taxonomyCraft", value)}
      />
      <Field
        label="前提模式"
        value={form.premisePattern}
        multiline
        onChange={(value) => change("premisePattern", value)}
      />
      <Field
        label="核心承诺"
        value={form.coreReaderPromise}
        multiline
        onChange={(value) => change("coreReaderPromise", value)}
      />
      <Field
        label="开篇合同"
        value={form.openingContract}
        multiline
        onChange={(value) => change("openingContract", value)}
      />
      <Field
        label="成长路线"
        value={form.progressionMap}
        multiline
        onChange={(value) => change("progressionMap", value)}
      />
      <Field
        label="循环场景"
        value={form.recurringLoops}
        multiline
        onChange={(value) => change("recurringLoops", value)}
      />
      <Field
        label="迁移边界"
        value={form.transferLimits}
        multiline
        onChange={(value) => change("transferLimits", value)}
      />
    </details>
  );
}

function VoiceFields({
  form,
  change,
}: {
  form: ReviewForm;
  change: ChangeReviewForm;
}) {
  return (
    <>
      <details className="knowledge-form-section" open>
        <summary>声音路由分类</summary>
        <Field
          label="平台"
          value={form.taxonomyPlatform}
          multiline
          onChange={(value) => change("taxonomyPlatform", value)}
        />
        <Field
          label="受众"
          value={form.taxonomyAudience}
          multiline
          onChange={(value) => change("taxonomyAudience", value)}
        />
        <Field
          label="题材分类"
          value={form.taxonomyGenre}
          multiline
          onChange={(value) => change("taxonomyGenre", value)}
        />
        <Field
          label="子题材"
          value={form.taxonomySubgenre}
          multiline
          onChange={(value) => change("taxonomySubgenre", value)}
        />
        <Field
          label="风格分类"
          value={form.taxonomyStyle}
          multiline
          onChange={(value) => change("taxonomyStyle", value)}
        />
        <Field
          label="视角分类"
          value={form.taxonomyPov}
          multiline
          onChange={(value) => change("taxonomyPov", value)}
        />
        <Field
          label="内容边界"
          value={form.taxonomyContentBoundary}
          multiline
          onChange={(value) => change("taxonomyContentBoundary", value)}
        />
      </details>
      <details className="knowledge-form-section" open>
        <summary>声音执行规则</summary>
        <Field
          label="视角距离"
          value={form.povDistance}
          multiline
          onChange={(value) => change("povDistance", value)}
        />
        <Field
          label="句式节奏"
          value={form.sentenceRhythm}
          multiline
          onChange={(value) => change("sentenceRhythm", value)}
        />
        <Field
          label="用词池"
          value={form.dictionPool}
          multiline
          onChange={(value) => change("dictionPool", value)}
        />
        <Field
          label="对话行为"
          value={form.dialogueBehavior}
          multiline
          onChange={(value) => change("dialogueBehavior", value)}
        />
        <Field
          label="描写行为"
          value={form.descriptionBehavior}
          multiline
          onChange={(value) => change("descriptionBehavior", value)}
        />
        <Field
          label="情绪处理"
          value={form.emotionHandling}
          multiline
          onChange={(value) => change("emotionHandling", value)}
        />
        <Field
          label="自然网文表达"
          value={form.humanWebProse}
          multiline
          onChange={(value) => change("humanWebProse", value)}
        />
        <Field
          label="句式变化"
          value={form.sentenceVariety}
          multiline
          onChange={(value) => change("sentenceVariety", value)}
        />
        <Field
          label="场景锚点规则"
          value={form.sceneAnchorRules}
          multiline
          onChange={(value) => change("sceneAnchorRules", value)}
        />
        <Field
          label="对话压力规则"
          value={form.dialoguePressureRules}
          multiline
          onChange={(value) => change("dialoguePressureRules", value)}
        />
        <Field
          label="AI 痕迹失败特征"
          value={form.antiAiFailureSignatures}
          multiline
          onChange={(value) => change("antiAiFailureSignatures", value)}
        />
        <Field
          label="可读性验收测试"
          value={form.readabilityAcceptanceTests}
          multiline
          onChange={(value) => change("readabilityAcceptanceTests", value)}
        />
        <Field
          label="禁止复制内容"
          value={form.doNotCopy}
          multiline
          onChange={(value) => change("doNotCopy", value)}
        />
      </details>
      <details className="knowledge-form-section" open>
        <summary>NLCL 声音路由</summary>
        <Field
          label="NLCL 路由层"
          value={form.nlclLayers}
          multiline
          onChange={(value) => change("nlclLayers", value)}
        />
        <label className="knowledge-field">
          <span>反馈层</span>
          <input value="L5_DEAI_STYLE" readOnly />
        </label>
      </details>
    </>
  );
}

function newValidatorExecutionRule(
  rules: ValidatorExecutionRuleForm[],
): ValidatorExecutionRuleForm {
  const usedIds = new Set(rules.map((rule) => rule.ruleId));
  let number = 1;
  while (usedIds.has(`validator_rule_${String(number).padStart(2, "0")}`))
    number += 1;
  const ruleId = `validator_rule_${String(number).padStart(2, "0")}`;
  return {
    ruleId,
    kind: "forbid_terms",
    terms: "",
    caseSensitive: false,
    failureSignature: `${ruleId}_failed`,
    message: "",
  };
}

function ValidatorFields({
  form,
  change,
}: {
  form: ReviewForm;
  change: ChangeReviewForm;
}) {
  function updateRule<K extends keyof ValidatorExecutionRuleForm>(
    index: number,
    key: K,
    value: ValidatorExecutionRuleForm[K],
  ) {
    change(
      "validatorExecutionRules",
      form.validatorExecutionRules.map((rule, ruleIndex) =>
        ruleIndex === index ? { ...rule, [key]: value } : rule,
      ),
    );
  }

  function addRule() {
    if (form.validatorExecutionRules.length >= 16) return;
    change("validatorExecutionRules", [
      ...form.validatorExecutionRules,
      newValidatorExecutionRule(form.validatorExecutionRules),
    ]);
  }

  function removeRule(index: number) {
    change(
      "validatorExecutionRules",
      form.validatorExecutionRules.filter(
        (_, ruleIndex) => ruleIndex !== index,
      ),
    );
  }

  return (
    <>
      <details className="knowledge-form-section" open>
        <summary>校验路由分类</summary>
        <Field
          label="平台"
          value={form.taxonomyPlatform}
          multiline
          onChange={(value) => change("taxonomyPlatform", value)}
        />
        <Field
          label="受众"
          value={form.taxonomyAudience}
          multiline
          onChange={(value) => change("taxonomyAudience", value)}
        />
        <Field
          label="题材分类"
          value={form.taxonomyGenre}
          multiline
          onChange={(value) => change("taxonomyGenre", value)}
        />
        <Field
          label="子题材"
          value={form.taxonomySubgenre}
          multiline
          onChange={(value) => change("taxonomySubgenre", value)}
        />
        <Field
          label="问题症状"
          value={form.taxonomyProblemSymptom}
          multiline
          onChange={(value) => change("taxonomyProblemSymptom", value)}
        />
        <Field
          label="内容边界"
          value={form.taxonomyContentBoundary}
          multiline
          onChange={(value) => change("taxonomyContentBoundary", value)}
        />
      </details>
      <details className="knowledge-form-section" open>
        <summary>校验器合同</summary>
        <Field
          label="校验器名称"
          value={form.validatorName}
          onChange={(value) => change("validatorName", value)}
        />
        <Field
          label="适用对象"
          value={form.validatorAppliesTo}
          multiline
          onChange={(value) => change("validatorAppliesTo", value)}
        />
        <label className="knowledge-field">
          <span>执行层</span>
          <select
            value={form.validatorLayerId}
            onChange={(event) => change("validatorLayerId", event.target.value)}
          >
            {validatorLayers.map((layer) => (
              <option key={layer} value={layer}>
                {layer}
              </option>
            ))}
          </select>
        </label>
        <Field
          label="检查项"
          value={form.validatorChecks}
          multiline
          onChange={(value) => change("validatorChecks", value)}
        />
        <Field
          label="拒收条件"
          value={form.validatorRejectConditions}
          multiline
          onChange={(value) => change("validatorRejectConditions", value)}
        />
        <Field
          label="重写路由"
          value={form.validatorRewriteRoutes}
          multiline
          onChange={(value) => change("validatorRewriteRoutes", value)}
        />
      </details>
      <details className="knowledge-form-section" open>
        <summary>正文执行规则</summary>
        <div className="knowledge-rule-toolbar">
          <span>{form.validatorExecutionRules.length} / 16</span>
          <button
            type="button"
            className="quiet-button"
            disabled={form.validatorExecutionRules.length >= 16}
            onClick={addRule}
          >
            <Plus size={13} />
            添加执行规则
          </button>
        </div>
        <div className="knowledge-rule-list">
          {form.validatorExecutionRules.map((rule, index) => (
            <fieldset
              className="knowledge-rule"
              aria-label={`执行规则 ${index + 1}`}
              key={index}
            >
              <legend>
                <span>规则 {index + 1}</span>
                <button
                  type="button"
                  className="quiet-button knowledge-rule-remove"
                  aria-label={`删除执行规则 ${index + 1}`}
                  title={`删除执行规则 ${index + 1}`}
                  onClick={() => removeRule(index)}
                >
                  <Trash2 size={13} />
                </button>
              </legend>
              <div className="knowledge-rule-grid">
                <label className="knowledge-field">
                  <span>规则 ID</span>
                  <input
                    value={rule.ruleId}
                    pattern="[a-z][a-z0-9_]*"
                    maxLength={64}
                    autoCapitalize="none"
                    spellCheck={false}
                    onChange={(event) =>
                      updateRule(index, "ruleId", event.target.value)
                    }
                  />
                </label>
                <label className="knowledge-field">
                  <span>规则类型</span>
                  <select
                    value={rule.kind}
                    onChange={(event) =>
                      updateRule(
                        index,
                        "kind",
                        event.target.value as ValidatorExecutionRuleKind,
                      )
                    }
                  >
                    <option value="forbid_terms">禁止出现任一词项</option>
                    <option value="require_any_term">必须出现至少一项</option>
                  </select>
                </label>
              </div>
              <Field
                label="匹配词项"
                value={rule.terms}
                multiline
                onChange={(value) => updateRule(index, "terms", value)}
              />
              <label className="knowledge-rule-toggle">
                <input
                  type="checkbox"
                  checked={rule.caseSensitive}
                  onChange={(event) =>
                    updateRule(index, "caseSensitive", event.target.checked)
                  }
                />
                <span>区分大小写</span>
              </label>
              <label className="knowledge-field">
                <span>失败签名</span>
                <input
                  value={rule.failureSignature}
                  pattern="[a-z][a-z0-9_]*"
                  maxLength={128}
                  autoCapitalize="none"
                  spellCheck={false}
                  onChange={(event) =>
                    updateRule(index, "failureSignature", event.target.value)
                  }
                />
              </label>
              <Field
                label="失败提示"
                value={rule.message}
                multiline
                onChange={(value) => updateRule(index, "message", value)}
              />
            </fieldset>
          ))}
        </div>
      </details>
      <details className="knowledge-form-section" open>
        <summary>NLCL 校验路由</summary>
        <Field
          label="NLCL 路由层"
          value={form.nlclLayers}
          multiline
          onChange={(value) => change("nlclLayers", value)}
        />
        <label className="knowledge-field">
          <span>反馈层</span>
          <input value={form.validatorLayerId} readOnly />
        </label>
      </details>
    </>
  );
}

function KnowledgeCandidateEditor({
  candidate,
  existingCard,
  batchFinalized,
  canReject,
  busy,
  onSave,
  onPromote,
  onReject,
}: {
  candidate: KnowledgeCandidate;
  existingCard: KnowledgeCard | null;
  batchFinalized: boolean;
  canReject: boolean;
  busy: boolean;
  onSave: (card: Record<string, unknown>) => Promise<unknown>;
  onPromote: (replaceActive: boolean) => Promise<unknown>;
  onReject: () => Promise<unknown>;
}) {
  const [form, setForm] = useState(() => formFromCandidate(candidate));
  const [dirty, setDirty] = useState(false);
  const [replaceConfirmed, setReplaceConfirmed] = useState(false);
  const [reviewConfirmed, setReviewConfirmed] = useState(false);
  useEffect(() => {
    setForm(formFromCandidate(candidate));
    setDirty(false);
    setReplaceConfirmed(false);
    setReviewConfirmed(false);
  }, [candidate]);
  const change = <K extends keyof ReviewForm>(key: K, value: ReviewForm[K]) => {
    setForm((current) => ({ ...current, [key]: value }));
    setReviewConfirmed(false);
    setDirty(true);
  };
  const reviewProblems = useMemo(
    () => reviewCompletionProblems(candidate, form),
    [candidate, form],
  );
  const currentCardIdIssue = cardIdProblem(form.cardId);
  const canSaveAsFormalReview = reviewConfirmed && reviewProblems.length === 0;
  const formalReady = candidate.issues.length === 0 && !dirty;
  const nextVersionNumber = existingCard
    ? Math.max(
        ...existingCard.versions.map((version) => version.version_number),
      ) + 1
    : 1;
  const label = candidateTypeLabel(candidate);

  return (
    <div className="knowledge-editor">
      <div className="knowledge-editor-heading">
        <div>
          <strong>
            {label.title}：{String(candidate.card.title ?? candidate.card_id)}
          </strong>
          <span>{label.subtitle}</span>
        </div>
        <span className={`candidate-state state-${candidate.status}`}>
          {candidate.status === "promoted"
            ? batchFinalized
              ? "已正式化"
              : "待整批正式化"
            : candidate.status === "rejected"
              ? "已拒绝"
              : "待复核"}
        </span>
      </div>

      <div className="knowledge-editor-context">
        <p>{candidateSummary(candidate)}</p>
        <span>{sourceAnchorText(candidate)}</span>
      </div>

      {candidate.issues.length > 0 && (
        <div className="knowledge-issues" role="status">
          <div>
            <AlertTriangle size={14} />
            <strong>{candidate.issues.length} 项未通过</strong>
          </div>
          {candidate.issues.slice(0, 8).map((issue) => (
            <p key={`${issue.field}-${issue.code}`}>
              <code>{issue.field}</code>
              <span>{issueText(issue)}</span>
            </p>
          ))}
        </div>
      )}

      {existingCard && candidate.status === "pending" && (
        <div className="knowledge-issues knowledge-replacement" role="status">
          <div>
            <History size={14} />
            <strong>需要明确替换现行版本</strong>
          </div>
          <p>
            <code>v{existingCard.active_version.version_number}</code>
            <span>
              同一 card_id 已有正式卡；确认后将创建 v{nextVersionNumber}
              ，旧版本仍可回滚。
            </span>
          </p>
          <label>
            <input
              type="checkbox"
              checked={replaceConfirmed}
              onChange={(event) => setReplaceConfirmed(event.target.checked)}
            />
            <span>我确认把此候选晋升为新的现行版本</span>
          </label>
        </div>
      )}

      <section className="knowledge-form-section">
        <h3>卡片摘要</h3>
        <Field
          label="card_id"
          value={form.cardId}
          onChange={(value) => change("cardId", value)}
        />
        {currentCardIdIssue && (
          <p className="knowledge-inline-warning" role="status">
            {currentCardIdIssue}
          </p>
        )}
        <Field
          label="标题"
          value={form.title}
          onChange={(value) => change("title", value)}
        />
        <Field
          label="摘要"
          value={form.summary}
          multiline
          onChange={(value) => change("summary", value)}
        />
        <Field
          label="读者承诺"
          value={form.readerPromise}
          multiline
          onChange={(value) => change("readerPromise", value)}
        />
        <Field
          label="标签"
          value={form.tags}
          multiline
          onChange={(value) => change("tags", value)}
        />
        <Field
          label="关键词"
          value={form.keywords}
          multiline
          onChange={(value) => change("keywords", value)}
        />
        <Field
          label="适用任务"
          value={form.applicableTasks}
          multiline
          onChange={(value) => change("applicableTasks", value)}
        />
        <div className="knowledge-field-row">
          <Field
            label="来源范围"
            value={form.sourceScope}
            onChange={(value) => change("sourceScope", value)}
          />
          <label className="knowledge-field">
            <span>证据强度</span>
            <select
              value={form.evidenceQuality}
              onChange={(event) =>
                change(
                  "evidenceQuality",
                  event.target.value as ReviewForm["evidenceQuality"],
                )
              }
            >
              <option value="weak">弱</option>
              <option value="medium">中</option>
              <option value="strong">强</option>
            </select>
          </label>
          <Field
            label="证据说明"
            value={form.evidenceNotes}
            onChange={(value) => change("evidenceNotes", value)}
          />
        </div>
      </section>

      {candidate.card_type === "work" && (
        <WorkFields form={form} change={change} />
      )}
      {candidate.card_type === "voice" && (
        <VoiceFields form={form} change={change} />
      )}
      {candidate.card_type === "validator" && (
        <ValidatorFields form={form} change={change} />
      )}

      <details className="knowledge-form-section">
        <summary>路由与安全边界</summary>
        <Field
          label="推荐 profiles"
          value={form.routeRecommendedProfiles}
          multiline
          onChange={(value) => change("routeRecommendedProfiles", value)}
        />
        <Field
          label="路由词"
          value={form.routeTerms}
          multiline
          onChange={(value) => change("routeTerms", value)}
        />
        <Field
          label="负路由"
          value={form.negativeRoutes}
          multiline
          onChange={(value) => change("negativeRoutes", value)}
        />
        <Field
          label="版权边界"
          value={form.safetyCopyrightBoundary}
          multiline
          onChange={(value) => change("safetyCopyrightBoundary", value)}
        />
        <Field
          label="禁止复用边界"
          value={form.safetyDoNotReuse}
          multiline
          onChange={(value) => change("safetyDoNotReuse", value)}
        />
      </details>

      <details className="knowledge-form-section">
        <summary>可执行机制</summary>
        <Field
          label="机制名"
          value={form.mechanicName}
          onChange={(value) => change("mechanicName", value)}
        />
        <Field
          label="适用时机"
          value={form.mechanicUseWhen}
          multiline
          onChange={(value) => change("mechanicUseWhen", value)}
        />
        <Field
          label="规则"
          value={form.mechanicRule}
          multiline
          onChange={(value) => change("mechanicRule", value)}
        />
        <Field
          label="失败模式"
          value={form.mechanicFailure}
          multiline
          onChange={(value) => change("mechanicFailure", value)}
        />
      </details>

      <details className="knowledge-form-section">
        <summary>读者效果</summary>
        <Field
          label="作者意图"
          value={form.authorIntent}
          multiline
          onChange={(value) => change("authorIntent", value)}
        />
        <Field
          label="观察效果"
          value={form.observedEffect}
          multiline
          onChange={(value) => change("observedEffect", value)}
        />
        <Field
          label="效果证据"
          value={form.effectEvidence}
          multiline
          onChange={(value) => change("effectEvidence", value)}
        />
        <Field
          label="失效边界"
          value={form.effectFailure}
          multiline
          onChange={(value) => change("effectFailure", value)}
        />
        <Field
          label="误读风险"
          value={form.misreadRisk}
          multiline
          onChange={(value) => change("misreadRisk", value)}
        />
        <Field
          label="转坏节点"
          value={form.turnsBad}
          multiline
          onChange={(value) => change("turnsBad", value)}
        />
      </details>

      <details className="knowledge-form-section">
        <summary>验证与评分</summary>
        <Field
          label="验收测试"
          value={form.validationTests}
          multiline
          onChange={(value) => change("validationTests", value)}
        />
        <div className="knowledge-ladder">
          {ladderSteps.map(([step, label]) => (
            <div key={step}>
              <label>
                <input
                  type="checkbox"
                  checked={form.ladderPassed[step]}
                  onChange={(event) =>
                    change("ladderPassed", {
                      ...form.ladderPassed,
                      [step]: event.target.checked,
                    })
                  }
                />
                <span>{label}</span>
              </label>
              <textarea
                aria-label={`${label}证据`}
                value={form.ladderEvidence[step]}
                onChange={(event) =>
                  change("ladderEvidence", {
                    ...form.ladderEvidence,
                    [step]: event.target.value,
                  })
                }
              />
            </div>
          ))}
        </div>
        <div className="knowledge-scores">
          {(
            [
              ["evidenceScore", "证据"],
              ["specificityScore", "具体"],
              ["generativityScore", "生成"],
              ["retrievalScore", "检索"],
              ["safetyRiskScore", "风险"],
            ] as const
          ).map(([key, label]) => (
            <label key={key}>
              <span>{label}</span>
              <input
                type="number"
                min="0"
                max="1"
                step="0.05"
                value={form[key]}
                onChange={(event) => change(key, event.target.value)}
              />
            </label>
          ))}
        </div>
        <div
          className={`knowledge-review-confirmation${canSaveAsFormalReview ? " ready" : ""}`}
        >
          <label>
            <input
              type="checkbox"
              checked={reviewConfirmed}
              onChange={(event) => {
                setReviewConfirmed(event.target.checked);
                setDirty(true);
              }}
            />
            <span>
              我已逐项确认这张候选的字段、证据、验证和评分，可按当前内容尝试解除草案标记。
            </span>
          </label>
          {reviewProblems.length > 0 ? (
            <div className="knowledge-review-blockers" role="status">
              <strong>仍会保存为草案，暂不能晋升：</strong>
              <ul>
                {reviewProblems.slice(0, 6).map((problem) => (
                  <li key={problem}>{problem}</li>
                ))}
              </ul>
              {reviewProblems.length > 6 && (
                <small>还有 {reviewProblems.length - 6} 项需要补齐。</small>
              )}
            </div>
          ) : (
            <p>
              字段与验证条件已齐备；勾选确认后保存，Runtime
              会再次复核是否允许晋升。
            </p>
          )}
        </div>
      </details>

      <footer className="knowledge-editor-actions">
        {canReject && (
          <button
            type="button"
            className="quiet-button"
            disabled={busy}
            onClick={() => void onReject().catch(() => undefined)}
          >
            <CircleX size={14} />
            拒绝候选
          </button>
        )}
        <button
          type="button"
          className="quiet-button"
          disabled={
            busy ||
            !dirty ||
            candidate.status !== "pending" ||
            currentCardIdIssue !== null
          }
          onClick={() =>
            void onSave(reviewedCard(candidate, form, reviewConfirmed)).catch(
              () => undefined,
            )
          }
        >
          {busy ? (
            <LoaderCircle size={14} className="spin" />
          ) : (
            <Save size={14} />
          )}
          {canSaveAsFormalReview ? "保存正式复核" : "保存复核草案"}
        </button>
        <button
          type="button"
          className="knowledge-promote"
          disabled={
            busy ||
            !formalReady ||
            candidate.status !== "pending" ||
            (existingCard !== null && !replaceConfirmed)
          }
          onClick={() =>
            void onPromote(existingCard !== null).catch(() => undefined)
          }
        >
          <ShieldCheck size={14} />
          {existingCard ? `晋升为 v${nextVersionNumber}` : "晋升正式卡"}
        </button>
      </footer>

      <details className="knowledge-technical-evidence">
        <summary>技术证据</summary>
        <dl>
          <div>
            <dt>candidate_id</dt>
            <dd>{candidate.candidate_id}</dd>
          </div>
          <div>
            <dt>candidate_card_id</dt>
            <dd>{candidate.card_id}</dd>
          </div>
          <div>
            <dt>current_form_card_id</dt>
            <dd>{form.cardId}</dd>
          </div>
          <div>
            <dt>revision</dt>
            <dd>{candidate.revision}</dd>
          </div>
          <div>
            <dt>content_hash</dt>
            <dd>{candidate.content_hash}</dd>
          </div>
          <div>
            <dt>origin_hash</dt>
            <dd>{candidate.origin?.origin_hash ?? "legacy"}</dd>
          </div>
          <div>
            <dt>source_draft_hash</dt>
            <dd>{candidate.source_draft_hash}</dd>
          </div>
        </dl>
      </details>
    </div>
  );
}

function FormalCardRow({
  card,
  busy,
  inherited,
  onToggle,
  onRollback,
}: {
  card: KnowledgeCard;
  busy: boolean;
  inherited: boolean;
  onToggle: () => Promise<unknown>;
  onRollback: (versionId: string) => Promise<unknown>;
}) {
  const activeIsFormalized =
    card.active_version.formalization_status === "verified";
  const older = card.versions.filter(
    (item) =>
      item.version_id !== card.active_version_id &&
      item.formalization_status === "verified",
  );
  const versionState = inherited
    ? "应用基础 · 只读"
    : card.active_version.formalization_status === "awaiting_formalization"
      ? "待整批正式化"
      : card.active_version.formalization_status === "abandoned"
        ? "已放弃"
        : card.active_version.formalization_status === "legacy_unverified"
          ? "legacy_unverified"
          : card.status === "active"
            ? "启用"
            : "停用";
  return (
    <article className="formal-card-row">
      <header>
        <div>
          <strong>
            {String(card.active_version.card.title ?? card.card_id)}
          </strong>
          <span>
            {card.card_id} · v{card.active_version.version_number} ·{" "}
            {card.active_version.lineage_status}
          </span>
        </div>
        <span className={`formal-status status-${card.status}`}>
          {versionState}
        </span>
      </header>
      <p>{String(card.active_version.card.summary ?? "")}</p>
      <div className="formal-card-actions">
        <button
          type="button"
          className="icon-button"
          title={
            inherited
              ? "应用基础卡由应用知识范围管理"
              : activeIsFormalized
                ? card.status === "active"
                  ? "停用正式卡"
                  : "启用正式卡"
                : "待整批正式化，暂不可切换状态"
          }
          aria-label={
            inherited
              ? `${card.card_id} 应用基础卡只读`
              : card.status === "active"
                ? "停用正式卡"
                : "启用正式卡"
          }
          disabled={inherited || busy || !activeIsFormalized}
          onClick={() => void onToggle().catch(() => undefined)}
        >
          {card.status === "active" ? (
            <Archive size={14} />
          ) : (
            <BookCheck size={14} />
          )}
        </button>
        {older.map((version) => (
          <button
            type="button"
            className="version-button"
            title={
              inherited ? "应用基础卡由应用知识范围管理" : version.content_hash
            }
            aria-label={`回滚 ${card.card_id} 到 v${version.version_number}`}
            key={version.version_id}
            disabled={inherited || busy}
            onClick={() =>
              void onRollback(version.version_id).catch(() => undefined)
            }
          >
            <RotateCcw size={12} />v{version.version_number}
          </button>
        ))}
      </div>
    </article>
  );
}

export function KnowledgeReviewPanel({
  enabled = true,
  sourceImportId = null,
  projectId = null,
  projectRevision = null,
  projectFingerprint = null,
  onClearSourceImport,
  onStartWriting,
  startWritingBusy = false,
}: {
  enabled?: boolean;
  sourceImportId?: string | null;
  projectId?: string | null;
  projectRevision?: number | null;
  projectFingerprint?: string | null;
  onClearSourceImport?: () => void;
  onStartWriting?: (selector: FormalKnowledgeSelector) => Promise<void>;
  startWritingBusy?: boolean;
}) {
  const review = useKnowledgeReview(enabled, sourceImportId, projectId);
  const projectKb = useProjectKbState({
    enabled,
    projectId,
    projectRevision,
    projectFingerprint,
    scopeStateRevision: review.scope?.state_revision ?? null,
    knowledgeFingerprint: review.fingerprint,
  });
  const projectKbBinding = useMemo(
    () =>
      projectKb.available &&
      projectId &&
      projectRevision !== null &&
      projectFingerprint
        ? currentProjectKbBinding(projectKb.record, {
            projectId,
            projectRevision,
            projectFingerprint,
            knowledgeScopeStateRevision: review.scope?.state_revision ?? null,
            knowledgeFingerprint: review.fingerprint,
          })
        : null,
    [
      projectFingerprint,
      projectId,
      projectKb.available,
      projectKb.record,
      projectRevision,
      review.fingerprint,
      review.scope?.state_revision,
    ],
  );
  const projectKbStartMessage = projectKbBinding
    ? null
    : projectKb.state === "loading" || projectKb.state === "rebuilding"
      ? "正在校验项目知识快照，完成后才能开始写作。"
      : projectKb.record?.status === "tampered"
        ? "项目知识快照校验失败，请先诊断并重新校验。"
        : projectKb.record?.status === "current"
          ? "current pointer 与 Manifest 不完整或不一致，请先重新校验。"
          : "请先重建或校验项目知识快照，状态为 current 后才能开始写作。";
  const selected = review.selected;
  const busy = review.action !== null || review.state === "loading";
  const pendingCount = useMemo(
    () => review.candidates.filter((item) => item.status === "pending").length,
    [review.candidates],
  );
  const statusCounts = useMemo(
    () => ({
      pending: review.candidates.filter((item) => item.status === "pending")
        .length,
      promoted: review.candidates.filter((item) => item.status === "promoted")
        .length,
      deferred: review.candidates.filter((item) => item.status === "deferred")
        .length,
      rejected: review.candidates.filter((item) => item.status === "rejected")
        .length,
    }),
    [review.candidates],
  );
  const inheritedCardIds = useMemo(
    () => new Set(review.inheritedCardIds),
    [review.inheritedCardIds],
  );
  const formalCardCount = useMemo(
    () =>
      review.cards.filter(
        (card) => card.active_version.formalization_status === "verified",
      ).length,
    [review.cards],
  );
  const batchFinalized = review.formalization?.status === "verified";
  const sourceVerified =
    review.sourceReceipt?.lineage_status === "verified" &&
    review.sourceReceipt.receipt !== null;
  const canRejectSelected =
    selected !== null &&
    sourceImportId !== null &&
    !batchFinalized &&
    ["pending", "deferred", "promoted"].includes(selected.status);
  const existingCard = useMemo(
    () =>
      selected
        ? (review.cards.find(
            (card) =>
              card.card_id === selected.card_id &&
              !inheritedCardIds.has(card.card_id),
          ) ?? null)
        : null,
    [inheritedCardIds, review.cards, selected],
  );
  const formalKnowledgeSelector =
    useMemo<FormalKnowledgeSelector | null>(() => {
      const receipt = review.formalization?.receipt;
      if (
        !projectId ||
        !sourceImportId ||
        !review.fingerprint ||
        review.formalization?.status !== "verified" ||
        !receipt ||
        receipt.source_import_id !== sourceImportId
      )
        return null;

      const promoted = receipt.decisions.filter(
        (decision) =>
          decision.decision === "promoted" && decision.promoted_version_id,
      );
      if (promoted.length === 0) return null;
      const cards = promoted.map((decision) => {
        const candidate = review.candidates.find(
          (item) => item.candidate_id === decision.candidate_id,
        );
        if (
          !candidate ||
          candidate.status !== "promoted" ||
          !decision.promoted_version_id ||
          candidate.promoted_version_id !== decision.promoted_version_id
        )
          return null;
        const card = review.cards.find(
          (item) => item.card_id === candidate.card_id,
        );
        const version = card?.versions.find(
          (item) => item.version_id === decision.promoted_version_id,
        );
        if (
          !card ||
          !version ||
          card.status !== "active" ||
          card.active_version_id !== version.version_id ||
          version.source_candidate_id !== candidate.candidate_id ||
          version.formalization_status !== "verified" ||
          version.formalization?.formalization_receipt_id !==
            receipt.formalization_receipt_id ||
          version.formalization.formalization_receipt_hash !==
            receipt.receipt_hash
        )
          return null;
        return {
          card_id: card.card_id,
          version_id: version.version_id,
          content_hash: version.content_hash,
        };
      });
      if (cards.some((item) => item === null)) return null;
      const selectedCards = cards
        .filter((item) => item !== null)
        .sort((left, right) => left.card_id.localeCompare(right.card_id));
      if (
        new Set(selectedCards.map((item) => item.card_id)).size !==
        selectedCards.length
      ) {
        return null;
      }
      return {
        schema: "zhimeng.formal-knowledge-selector.v1",
        source_import_id: sourceImportId,
        formalization_receipt_id: receipt.formalization_receipt_id,
        formalization_receipt_hash: receipt.receipt_hash,
        expected_knowledge_fingerprint: review.fingerprint,
        cards: selectedCards,
      };
    }, [
      projectId,
      review.candidates,
      review.cards,
      review.fingerprint,
      review.formalization,
      sourceImportId,
    ]);

  return (
    <div
      className="inspector-body knowledge-panel"
      role="tabpanel"
      aria-busy={busy}
    >
      <header className="knowledge-toolbar">
        <Database size={16} />
        <div>
          <strong>原生知识库</strong>
          <span>
            {pendingCount} 待复核 · {formalCardCount} 正式卡
          </span>
          <span title={review.scope?.scope_id}>
            {review.scopeLabel} · fingerprint{" "}
            {review.scopeFingerprint?.slice(0, 8) ?? "读取中"}
          </span>
        </div>
        {sourceImportId && onClearSourceImport && (
          <button
            type="button"
            className="quiet-button"
            title={sourceImportId}
            onClick={onClearSourceImport}
          >
            <FilterX size={14} />
            查看全部候选
          </button>
        )}
        <button
          type="button"
          className="icon-button"
          title="刷新知识库"
          aria-label="刷新知识库"
          disabled={busy}
          onClick={() => void review.refresh()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      {projectKb.available && (
        <section
          className={`project-kb-status project-kb-${projectKb.record?.status ?? projectKb.state}`}
          aria-label="项目知识快照"
          aria-live="polite"
        >
          {projectKb.state === "loading" || projectKb.state === "rebuilding" ? (
            <LoaderCircle size={15} className="spin" />
          ) : projectKb.record?.status === "current" ? (
            <CheckCircle2 size={15} />
          ) : (
            <AlertTriangle size={15} />
          )}
          <div>
            <strong>
              {projectKb.state === "rebuilding"
                ? "正在重建项目知识快照"
                : projectKb.record?.status === "current"
                  ? "项目知识快照已同步"
                  : projectKb.record?.status === "stale"
                    ? "项目知识快照已过期"
                    : projectKb.record?.status === "tampered"
                      ? "项目知识快照校验失败"
                      : projectKb.record?.status === "missing"
                        ? "项目知识快照尚未生成"
                        : "正在读取项目知识快照"}
            </strong>
            <span>
              {projectKb.record?.manifest
                ? `${projectKb.record.manifest.counts.active_index_entries} 条有效索引 · ${projectKb.record.manifest.state_id.slice(-8)}`
                : "等待首次同步"}
            </span>
            {projectKb.record?.reasons.length ? (
              <small>
                {projectKb.record.reasons.map(projectKbReasonLabel).join(" · ")}
              </small>
            ) : null}
            {projectKb.error ? (
              <small role="alert">{projectKb.error}</small>
            ) : null}
          </div>
          {projectKb.record?.status !== "current" &&
            projectKb.record?.status !== "tampered" && (
              <button
                type="button"
                className="quiet-button"
                disabled={!projectKb.canRebuild}
                onClick={() => void projectKb.rebuild().catch(() => undefined)}
              >
                <RefreshCw size={13} />
                重建快照
              </button>
            )}
          {projectKb.record?.status === "current" && (
            <button
              type="button"
              className="icon-button"
              title="重新校验项目知识快照"
              aria-label="重新校验项目知识快照"
              disabled={projectKb.state === "loading"}
              onClick={() => void projectKb.refresh().catch(() => undefined)}
            >
              <RefreshCw size={13} />
            </button>
          )}
        </section>
      )}

      <KnowledgeWorkflowSteps
        sourceImportId={sourceImportId}
        sourceVerified={sourceVerified}
        candidatesReady={review.candidates.length > 0}
        reviewDone={review.candidates.length > 0 && pendingCount === 0}
        batchFinalized={batchFinalized}
      />

      {review.error && (
        <div className="knowledge-error" role="alert">
          <AlertTriangle size={14} />
          <span>{review.error}</span>
          {review.errorStatus === 409 &&
            selected?.status === "promoted" &&
            !batchFinalized && (
              <span>激活基线已经变化，请拒绝当前 staged 候选后重新导入。</span>
            )}
          {review.errorStatus === 409 && (
            <button
              type="button"
              className="quiet-button"
              onClick={() => void review.refresh()}
            >
              <RefreshCw size={13} />
              重新载入状态
            </button>
          )}
        </div>
      )}
      {sourceImportId && (
        <details className="knowledge-technical-evidence source-evidence">
          <summary>
            {sourceVerified ? (
              <CheckCircle2 size={14} />
            ) : (
              <AlertTriangle size={14} />
            )}
            <span>来源 {review.sourceReceipt?.lineage_status ?? "读取中"}</span>
            <em>技术证据</em>
          </summary>
          {review.sourceReceipt?.receipt ? (
            <dl>
              <div>
                <dt>receipt_id</dt>
                <dd>{review.sourceReceipt.receipt.receipt_id}</dd>
              </div>
              <div>
                <dt>receipt_hash</dt>
                <dd>{review.sourceReceipt.receipt.receipt_hash}</dd>
              </div>
              <div>
                <dt>cards_draft_hash</dt>
                <dd>{review.sourceReceipt.receipt.cards_draft_hash}</dd>
              </div>
              <div>
                <dt>candidate_set_hash</dt>
                <dd>{review.candidateSetHash ?? "未读取"}</dd>
              </div>
            </dl>
          ) : review.state === "ready" ? (
            <p>legacy 来源没有可验证执行收据，不能完成正式化。</p>
          ) : (
            <p>正在读取来源收据。</p>
          )}
        </details>
      )}
      {review.candidates.length > 0 && (
        <div
          className="knowledge-status-strip"
          role="status"
          aria-label="候选状态汇总"
        >
          <span>pending {statusCounts.pending}</span>
          <span>promoted {statusCounts.promoted}</span>
          <span>deferred {statusCounts.deferred}</span>
          <span>rejected {statusCounts.rejected}</span>
        </div>
      )}
      {review.state === "loading" && review.candidates.length === 0 && (
        <div className="knowledge-empty">
          <LoaderCircle size={16} className="spin" />
          <span>
            {review.action === "import" ? "正在载入蒸馏草案" : "正在读取知识库"}
          </span>
        </div>
      )}
      {review.state === "ready" && review.candidates.length === 0 && (
        <div className="knowledge-empty">
          <Database size={16} />
          <span>暂无知识候选</span>
        </div>
      )}

      {review.candidates.length > 0 && (
        <KnowledgeCandidateBoard
          candidates={review.candidates}
          selectedId={selected?.candidate_id ?? null}
          batchFinalized={batchFinalized}
          onSelect={review.select}
        />
      )}

      {selected?.status === "deferred" && (
        <div className="knowledge-deferred">
          <History size={16} />
          <div>
            <strong>{selected.card_type} 卡已保留</strong>
            <span>该卡未进入本次可晋升队列，不会自动正式化。</span>
            {canRejectSelected && (
              <button
                type="button"
                className="quiet-button"
                disabled={busy}
                onClick={() =>
                  void review.reject(selected).catch(() => undefined)
                }
              >
                <CircleX size={14} />
                拒绝候选
              </button>
            )}
          </div>
        </div>
      )}
      {selected?.status === "rejected" && (
        <div className="knowledge-deferred">
          <CircleX size={16} />
          <div>
            <strong>{selected.card_type} 卡已拒绝</strong>
            <span>该候选不会进入本批正式知识。</span>
          </div>
        </div>
      )}
      {selected && !["deferred", "rejected"].includes(selected.status) && (
        <KnowledgeCandidateEditor
          key={selected.candidate_id}
          candidate={selected}
          existingCard={existingCard}
          batchFinalized={batchFinalized}
          canReject={canRejectSelected}
          busy={busy}
          onSave={(card) => review.save(selected, card)}
          onPromote={(replaceActive) => review.promote(selected, replaceActive)}
          onReject={() => review.reject(selected)}
        />
      )}

      {sourceImportId &&
        sourceVerified &&
        review.formalization?.status === "awaiting_review" &&
        pendingCount === 0 && (
          <footer className="knowledge-editor-actions">
            <button
              type="button"
              className="knowledge-promote"
              disabled={busy || !review.candidateSetHash}
              onClick={() => void review.finalize().catch(() => undefined)}
            >
              {review.action === "finalize" ? (
                <LoaderCircle size={14} className="spin" />
              ) : (
                <ShieldCheck size={14} />
              )}
              完成本批复核
            </button>
          </footer>
        )}

      {review.formalization?.status === "verified" &&
        review.formalization.receipt && (
          <>
            <section
              className="knowledge-deferred"
              role="status"
              aria-label="资料批次已完成"
            >
              <CheckCircle2 size={16} />
              <div>
                <strong>completed · 本批复核已完成 · 三类知识已正式化</strong>
                <span>
                  {review.formalization.receipt.formalization_receipt_id}
                </span>
                <code title={review.formalization.receipt.receipt_hash}>
                  {review.formalization.receipt.receipt_hash}
                </code>
              </div>
            </section>
            {onStartWriting && (
              <footer className="knowledge-editor-actions knowledge-start-writing">
                {formalKnowledgeSelector && projectKbStartMessage && (
                  <span
                    className="knowledge-start-writing-status"
                    role="status"
                  >
                    {projectKbStartMessage}
                  </span>
                )}
                <button
                  type="button"
                  className="knowledge-promote"
                  title={
                    !formalKnowledgeSelector
                      ? "批次引用已变化，请刷新后重试"
                      : (projectKbStartMessage ??
                        "使用这批已验证知识编译写作上下文")
                  }
                  disabled={
                    !enabled ||
                    !projectId ||
                    busy ||
                    startWritingBusy ||
                    !formalKnowledgeSelector ||
                    !projectKbBinding
                  }
                  onClick={() => {
                    if (formalKnowledgeSelector && projectKbBinding)
                      void onStartWriting(formalKnowledgeSelector).catch(
                        () => undefined,
                      );
                  }}
                >
                  {startWritingBusy ? (
                    <LoaderCircle size={14} className="spin" />
                  ) : (
                    <Sparkles size={14} />
                  )}
                  使用这批知识开始写作
                </button>
              </footer>
            )}
          </>
        )}

      {review.cards.length > 0 && (
        <section className="formal-library">
          <div className="formal-library-heading">
            <div>
              <CheckCircle2 size={15} />
              <strong>知识版本</strong>
            </div>
            <code title={review.fingerprint ?? ""}>
              {review.fingerprint?.slice(0, 8)}
            </code>
          </div>
          {review.cards.map((card) => (
            <FormalCardRow
              key={card.card_id}
              card={card}
              busy={busy}
              inherited={inheritedCardIds.has(card.card_id)}
              onToggle={() =>
                review.setCardEnabled(card, card.status !== "active")
              }
              onRollback={(versionId) => review.rollback(card, versionId)}
            />
          ))}
        </section>
      )}
    </div>
  );
}

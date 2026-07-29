import {
  cleanup,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  finalizeKnowledgeSourceImport,
  finalizeProjectKnowledgeSourceImport,
  getProjectKbState,
  getKnowledgeSourceFormalization,
  getProjectKnowledgeSourceFormalization,
  importKnowledgeCandidates,
  importProjectKnowledgeCandidates,
  listKnowledgeCandidates,
  listKnowledgeCards,
  listProjectKnowledgeCandidates,
  listProjectKnowledgeCards,
  promoteKnowledgeCandidate,
  promoteProjectKnowledgeCandidate,
  rejectKnowledgeCandidate,
  rejectProjectKnowledgeCandidate,
  rebuildProjectKbState,
  rollbackKnowledgeCard,
  rollbackProjectKnowledgeCard,
  updateKnowledgeCandidate,
  updateProjectKnowledgeCandidate,
  type KnowledgeCandidate,
  type KnowledgeCard,
  type ProjectKbStateRecord,
} from "../api/knowledge";
import { RuntimeApiError } from "../api/runtime";
import { getSourceImportExecutionReceipt } from "../api/sourceImports";
import {
  knowledgeAwaitingFormalizationFixture,
  knowledgeCandidateFixture,
  knowledgeCandidateListFixture,
  knowledgeCardFixture,
  knowledgeCardListFixture,
  knowledgeFormalizationFixture,
  knowledgeFormalizationReceiptFixture,
  knowledgeProjectScopeFixture,
  knowledgeScopedCardListFixture,
  knowledgeSourceReceiptFixture,
  currentProjectKbStateFixture,
} from "../test/knowledgeFixture";
import { KnowledgeReviewPanel } from "./KnowledgeReviewPanel";

vi.mock("../api/knowledge", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/knowledge")>();
  return {
    ...actual,
    finalizeKnowledgeSourceImport: vi.fn(),
    finalizeProjectKnowledgeSourceImport: vi.fn(),
    getProjectKbState: vi.fn(),
    getKnowledgeSourceFormalization: vi.fn(),
    getProjectKnowledgeSourceFormalization: vi.fn(),
    importKnowledgeCandidates: vi.fn(),
    importProjectKnowledgeCandidates: vi.fn(),
    listKnowledgeCandidates: vi.fn(),
    listKnowledgeCards: vi.fn(),
    listProjectKnowledgeCandidates: vi.fn(),
    listProjectKnowledgeCards: vi.fn(),
    promoteKnowledgeCandidate: vi.fn(),
    promoteProjectKnowledgeCandidate: vi.fn(),
    rejectKnowledgeCandidate: vi.fn(),
    rejectProjectKnowledgeCandidate: vi.fn(),
    rebuildProjectKbState: vi.fn(),
    rollbackKnowledgeCard: vi.fn(),
    rollbackProjectKnowledgeCard: vi.fn(),
    updateKnowledgeCandidate: vi.fn(),
    updateProjectKnowledgeCandidate: vi.fn(),
  };
});

vi.mock("../api/sourceImports", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../api/sourceImports")>();
  return {
    ...actual,
    getSourceImportExecutionReceipt: vi.fn(),
  };
});

function readyCandidate(): KnowledgeCandidate {
  return {
    ...knowledgeCandidateFixture,
    card: {
      ...knowledgeCandidateFixture.card,
      draft_candidate: false,
      requires_human_distillation: false,
    },
    issues: [],
  };
}

function versionedCard(): KnowledgeCard {
  const currentVersion = {
    ...knowledgeCardFixture.active_version,
    version_id: `knowledge-version-${"f".repeat(32)}`,
    version_number: 2,
    content_hash: "3".repeat(64),
  };
  return {
    ...knowledgeCardFixture,
    state_revision: 2,
    active_version_id: currentVersion.version_id,
    active_version: currentVersion,
    versions: [currentVersion, knowledgeCardFixture.active_version],
  };
}

function voiceCandidate(): KnowledgeCandidate {
  return {
    ...knowledgeCandidateFixture,
    candidate_id: `knowledge-candidate-${"b".repeat(32)}`,
    card_id: "voice-native-fixture",
    card_type: "voice",
    card: {
      ...knowledgeCandidateFixture.card,
      card_id: "voice-native-fixture",
      card_type: "voice",
      title: "贴近人物压力的叙事声音规则",
      summary:
        "保持贴近行动人物判断的有限视角，用具体动作、关系站位和现场物件承接压力变化，让正文自然可读且不复制来源声纹。",
      reader_promise:
        "读者能顺畅读懂人物正在做什么、为什么犹豫以及选择会付出什么代价。",
      source: {
        ...(knowledgeCandidateFixture.card.source as Record<string, unknown>),
        scope: "完整作品跨章节结构与独立样本复核",
      },
      tags: ["番茄", "男频", "玄幻", "有限视角", "自然网文", "现场压力"],
      keywords: ["人物压力", "自然叙事"],
      applicable_tasks: ["draft_scene", "rewrite", "review"],
      taxonomy: {
        platform: ["番茄"],
        audience: ["男频"],
        genre: ["玄幻"],
        style: ["现场压力", "自然网文"],
        pov: ["贴近主角的有限视角"],
        content_boundary: ["只迁移抽象叙事行为"],
      },
      type_payload: {
        pov_distance: "贴近当前行动人物的判断，不直接替角色总结情绪。",
        sentence_rhythm: "动作处用短句落点，关系变化用中句承接。",
        diction_pool: ["具体动作与物件", "人物关系站位"],
        dialogue_behavior: "每句对话携带目的、关系站位或信息差。",
        description_behavior: "描写必须改变人物判断或当前行动空间。",
        emotion_handling: "先写身体反应、动作选择和关系后果。",
        human_web_prose: "句子语义完整、主谓清楚并落在当前场景。",
        sentence_variety: "短中长句按压力变化交替，不用连续碎句。",
        scene_anchor_rules: ["每两到三段出现一次会改变判断的现场锚点。"],
        dialogue_pressure_rules: ["每句对话至少推进目的、关系或信息差之一。"],
        anti_ai_failure_signatures: [
          "连续机械碎句",
          "抽象心理替代动作",
          "角色同声解释设定",
        ],
        readability_acceptance_tests: [
          "连续阅读时能分清谁在行动与谁在承担代价。",
        ],
        do_not_copy: ["来源作者标志句", "来源作品节奏链", "来源人物口头禅"],
      },
      nlcl_route_hints: {
        recommended_profiles: ["chapter", "review"],
        nlcl_layers: ["distillation_index", "style", "deai"],
        route_terms: ["人物压力", "自然叙事"],
        negative_routes: ["作者声纹模仿"],
        feedback_default_layer: "L3_DISTILLATION_RETRIEVAL",
      },
    },
    issues: [
      {
        code: "invalid_feedback_layer",
        field: "nlcl_route_hints.feedback_default_layer",
        message: "Voice cards must route feedback to L5_DEAI_STYLE",
      },
    ],
  };
}

function validatorCandidate(): KnowledgeCandidate {
  return {
    ...knowledgeCandidateFixture,
    candidate_id: `knowledge-candidate-${"c".repeat(32)}`,
    card_id: "validator-native-fixture",
    card_type: "validator",
    card: {
      ...knowledgeCandidateFixture.card,
      card_id: "validator-native-fixture",
      card_type: "validator",
      title: "人物压力与来源污染的正文拒收闸门",
      summary:
        "在正文候选进入作者审阅前检查人物压力是否落到动作和代价，并阻断来源名称、设定壳、标志句或可识别场景顺序进入当前项目。",
      reader_promise:
        "只有动作、关系和代价可追溯且没有来源污染的正文候选才能进入作者审阅。",
      source: {
        ...(knowledgeCandidateFixture.card.source as Record<string, unknown>),
        scope: "完整作品跨章节结构与独立样本复核",
      },
      tags: ["番茄", "男频", "玄幻", "正文审阅", "来源污染", "拒收闸门"],
      keywords: ["人物压力", "来源污染"],
      applicable_tasks: ["review", "rewrite"],
      taxonomy: {
        platform: ["番茄"],
        audience: ["男频"],
        genre: ["玄幻"],
        problem_symptom: ["抽象总结", "来源污染"],
        content_boundary: ["正文候选拒收闸门"],
      },
      type_payload: {
        validator_name: "character_pressure_and_source_contamination_gate",
        applies_to: ["完整章节正文候选", "针对性正文改写候选"],
        layer_id: "L6_REVIEW_ACCEPTANCE",
        checks: [
          "人物选择是否通过动作、关系或资源变化产生代价。",
          "正文是否避开来源名称、设定壳和标志句。",
        ],
        reject_conditions: [
          "连续使用抽象心理总结替代人物行动。",
          "出现来源专名、标志表达或完整场景顺序。",
        ],
        rewrite_routes: ["L4_DRAFT_EXECUTION", "L5_DEAI_STYLE"],
      },
      nlcl_route_hints: {
        recommended_profiles: ["chapter", "review", "rewrite"],
        nlcl_layers: ["distillation_index", "review"],
        route_terms: ["人物压力", "来源污染"],
        negative_routes: ["跳过拒收条件"],
        feedback_default_layer: "L6_REVIEW_ACCEPTANCE",
      },
    },
    issues: [
      {
        code: "invalid_validator_layer",
        field: "type_payload.layer_id",
        message: "Validator cards require one known NLCL execution layer",
      },
      {
        code: "missing_validator_execution_rules",
        field: "type_payload.execution_rules",
        message: "Add at least one bounded deterministic validator rule",
      },
    ],
  };
}

describe("KnowledgeReviewPanel", () => {
  afterEach(cleanup);

  beforeEach(() => {
    vi.resetAllMocks();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue(
      knowledgeCandidateListFixture,
    );
    vi.mocked(listKnowledgeCards).mockResolvedValue(knowledgeCardListFixture);
    vi.mocked(importKnowledgeCandidates).mockResolvedValue(
      knowledgeCandidateListFixture,
    );
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      knowledgeSourceReceiptFixture,
    );
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeAwaitingFormalizationFixture,
    );
    vi.mocked(finalizeKnowledgeSourceImport).mockResolvedValue(
      knowledgeFormalizationReceiptFixture,
    );
    vi.mocked(promoteKnowledgeCandidate).mockResolvedValue(
      knowledgeCardFixture,
    );
    vi.mocked(rejectKnowledgeCandidate).mockResolvedValue({
      ...knowledgeCandidateFixture,
      status: "rejected",
      revision: knowledgeCandidateFixture.revision + 1,
    });
    vi.mocked(rollbackKnowledgeCard).mockResolvedValue(knowledgeCardFixture);
    vi.mocked(updateKnowledgeCandidate).mockResolvedValue({
      ...knowledgeCandidateFixture,
      revision: 2,
      content_hash: "2".repeat(64),
      card: {
        ...knowledgeCandidateFixture.card,
        draft_candidate: false,
        requires_human_distillation: false,
      },
      issues: [],
    });
    vi.mocked(finalizeProjectKnowledgeSourceImport).mockResolvedValue(
      knowledgeFormalizationReceiptFixture,
    );
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeAwaitingFormalizationFixture,
    );
    vi.mocked(importProjectKnowledgeCandidates).mockResolvedValue(
      knowledgeCandidateListFixture,
    );
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue(
      knowledgeCandidateListFixture,
    );
    vi.mocked(listProjectKnowledgeCards).mockResolvedValue(
      knowledgeScopedCardListFixture,
    );
    vi.mocked(promoteProjectKnowledgeCandidate).mockResolvedValue(
      knowledgeCardFixture,
    );
    vi.mocked(rejectProjectKnowledgeCandidate).mockResolvedValue({
      ...knowledgeCandidateFixture,
      status: "rejected",
      revision: knowledgeCandidateFixture.revision + 1,
    });
    vi.mocked(rollbackProjectKnowledgeCard).mockResolvedValue(
      knowledgeCardFixture,
    );
    vi.mocked(updateProjectKnowledgeCandidate).mockResolvedValue(
      knowledgeCandidateFixture,
    );
  });

  it("shows candidate issues and the active formal library fingerprint", async () => {
    render(
      <KnowledgeReviewPanel
        sourceImportId={knowledgeCandidateFixture.source_import_id}
      />,
    );

    expect(await screen.findByText("1 项未通过")).toBeInTheDocument();
    expect(screen.getByText("阶段兑现循环")).toBeInTheDocument();
    expect(screen.getByText("来源 verified")).toBeInTheDocument();
    expect(screen.getByText("pending 1")).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "完成本批复核" }),
    ).not.toBeInTheDocument();
    expect(
      screen.getByTitle(knowledgeCardListFixture.fingerprint),
    ).toHaveTextContent("11111111");
  });

  it("shows the active project scope and scoped fingerprint", async () => {
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    render(<KnowledgeReviewPanel projectId={projectId} />);

    expect(
      await screen.findByText(
        "项目知识范围（含应用基础） · fingerprint 99999999",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle(knowledgeProjectScopeFixture.scope_id),
    ).toBeInTheDocument();
  });

  it("shows and rebuilds the Runtime-owned project knowledge snapshot", async () => {
    const user = userEvent.setup();
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    const projectFingerprint = "7".repeat(64);
    const missing: ProjectKbStateRecord = {
      schema: "zhimeng.project-kb-state-record.v1",
      project_id: projectId,
      status: "missing",
      runtime_relative_root: `project-kb/${projectId}.kb`,
      pointer: null,
      manifest: null,
      reasons: [],
    };
    const current = currentProjectKbStateFixture({
      projectId,
      revision: 4,
      fingerprint: projectFingerprint,
      stateId: `project-kb-state-${"8".repeat(32)}`,
      manifestHash: "9".repeat(64),
    });
    vi.mocked(getProjectKbState).mockResolvedValue(missing);
    vi.mocked(rebuildProjectKbState).mockResolvedValue({
      schema: "zhimeng.project-kb-rebuild-result.v1",
      reused_snapshot: false,
      record: current,
    });

    render(
      <KnowledgeReviewPanel
        projectId={projectId}
        projectRevision={4}
        projectFingerprint={projectFingerprint}
      />,
    );

    const rebuild = await screen.findByRole("button", { name: "重建快照" });
    await user.click(rebuild);

    expect(rebuildProjectKbState).toHaveBeenCalledWith(projectId, {
      expected_project_revision: 4,
      expected_project_fingerprint: projectFingerprint,
      expected_scope_state_revision:
        knowledgeScopedCardListFixture.scope.state_revision,
      expected_knowledge_fingerprint:
        knowledgeScopedCardListFixture.fingerprint,
    });
    expect(await screen.findByText("项目知识快照已同步")).toBeInTheDocument();
    expect(screen.getByText(/3 条有效索引/)).toBeInTheDocument();
  });

  it("keeps inherited application cards read-only and treats a same-id candidate as an overlay", async () => {
    const user = userEvent.setup();
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    const candidate = readyCandidate();
    const inheritedCard = versionedCard();
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listProjectKnowledgeCards).mockResolvedValue({
      ...knowledgeScopedCardListFixture,
      cards: [inheritedCard],
      inherited_card_ids: [inheritedCard.card_id],
    });
    render(<KnowledgeReviewPanel projectId={projectId} />);

    expect(await screen.findByText("应用基础 · 只读")).toBeInTheDocument();
    expect(
      screen.getByRole("button", {
        name: `${inheritedCard.card_id} 应用基础卡只读`,
      }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", {
        name: `回滚 ${inheritedCard.card_id} 到 v1`,
      }),
    ).toBeDisabled();

    await user.click(screen.getByRole("button", { name: "晋升正式卡" }));
    await waitFor(() =>
      expect(promoteProjectKnowledgeCandidate).toHaveBeenCalledWith(
        projectId,
        candidate,
        false,
      ),
    );
    expect(screen.queryByText("需要明确替换现行版本")).not.toBeInTheDocument();
  });

  it("keeps a saved edit as draft until the author explicitly confirms review gates", async () => {
    const user = userEvent.setup();
    render(
      <KnowledgeReviewPanel
        sourceImportId={knowledgeCandidateFixture.source_import_id}
      />,
    );
    const title = await screen.findByDisplayValue("阶段兑现循环");
    await user.type(title, "复核版");
    await user.click(screen.getByRole("button", { name: "保存复核草案" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const card = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(card?.draft_candidate).toBe(true);
    expect(card?.requires_human_distillation).toBe(true);
    expect(card?.summary).toBe(knowledgeCandidateFixture.card.summary);
    expect(card?.reader_promise).toBe(
      knowledgeCandidateFixture.card.reader_promise,
    );
  });

  it("fails closed on unsafe card ids until the author edits the visible card_id field", async () => {
    const user = userEvent.setup();
    const unsafeCandidate: KnowledgeCandidate = {
      ...readyCandidate(),
      card_id: "distilled:work:unsafe",
      card: {
        ...readyCandidate().card,
        card_id: "distilled:work:unsafe",
      },
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [unsafeCandidate],
    });
    vi.mocked(updateKnowledgeCandidate).mockResolvedValue({
      ...unsafeCandidate,
      revision: 2,
      content_hash: "4".repeat(64),
      card_id: "distilled-work-unsafe",
      card: {
        ...unsafeCandidate.card,
        card_id: "distilled-work-unsafe",
      },
      issues: [],
    });
    render(
      <KnowledgeReviewPanel
        sourceImportId={unsafeCandidate.source_import_id}
      />,
    );

    const cardId = await screen.findByRole("textbox", { name: "card_id" });
    expect(cardId).toHaveValue("distilled:work:unsafe");
    await user.type(screen.getByRole("textbox", { name: "标题" }), "作者复核");
    expect(screen.getAllByText(/不能静默清洗/).length).toBeGreaterThan(0);
    expect(screen.getByRole("button", { name: "保存复核草案" })).toBeDisabled();
    expect(updateKnowledgeCandidate).not.toHaveBeenCalled();

    await user.clear(cardId);
    await user.type(cardId, "distilled-work-unsafe");
    await user.click(screen.getByRole("button", { name: "保存复核草案" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const savedCard = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(savedCard?.card_id).toBe("distilled-work-unsafe");
  });

  it("does not use hidden provider taxonomy to satisfy review gates after visible fields are cleared", async () => {
    const user = userEvent.setup();
    const candidate = readyCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    const genre = await screen.findByRole("textbox", { name: "题材分类" });
    await user.clear(genre);
    await user.click(screen.getByRole("checkbox", { name: /我已逐项确认/ }));
    expect(screen.getByText("作品机制缺少 题材分类。")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "保存复核草案" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const savedCard = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(savedCard?.draft_candidate).toBe(true);
    expect((savedCard?.taxonomy as Record<string, unknown>)?.genre).toEqual([]);
  });

  it("round-trips all provider evidence lines instead of truncating to the first item", async () => {
    const user = userEvent.setup();
    const candidate: KnowledgeCandidate = {
      ...readyCandidate(),
      card: {
        ...readyCandidate().card,
        title: "阶段兑现循环正式复核",
        source: {
          ...(readyCandidate().card.source as Record<string, unknown>),
          scope: "完整作品跨章节结构与独立样本复核",
        },
        reader_effect: {
          ...(readyCandidate().card.reader_effect as Record<string, unknown>),
          effect_evidence: [
            "第一条读者效果证据覆盖选择代价。",
            "第二条读者效果证据覆盖后续债务。",
          ],
        },
        validation_ladder: Object.fromEntries(
          [
            "internal_consistency",
            "predictive_validation",
            "blind_sample_validation",
            "extreme_scenario_stress_test",
          ].map((step) => [
            step,
            {
              status: "pass",
              evidence: [`${step} 第一条证据。`, `${step} 第二条证据。`],
            },
          ]),
        ),
      },
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    expect(await screen.findByDisplayValue(/第一条读者效果证据/)).toHaveValue(
      "第一条读者效果证据覆盖选择代价。\n第二条读者效果证据覆盖后续债务。",
    );
    await user.click(screen.getByRole("checkbox", { name: /我已逐项确认/ }));
    await user.click(screen.getByRole("button", { name: "保存正式复核" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const savedCard = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(
      (savedCard?.reader_effect as Record<string, unknown>)?.effect_evidence,
    ).toEqual([
      "第一条读者效果证据覆盖选择代价。",
      "第二条读者效果证据覆盖后续债务。",
    ]);
    const ladder = savedCard?.validation_ladder as Record<
      string,
      { evidence: string[] }
    >;
    expect(ladder.internal_consistency.evidence).toEqual([
      "internal_consistency 第一条证据。",
      "internal_consistency 第二条证据。",
    ]);
    expect(ladder.extreme_scenario_stress_test.evidence).toEqual([
      "extreme_scenario_stress_test 第一条证据。",
      "extreme_scenario_stress_test 第二条证据。",
    ]);
  });

  it("resets explicit review confirmation when any field changes after confirmation", async () => {
    const user = userEvent.setup();
    const candidate: KnowledgeCandidate = {
      ...readyCandidate(),
      card: {
        ...readyCandidate().card,
        title: "阶段兑现循环正式复核",
        source: {
          ...(readyCandidate().card.source as Record<string, unknown>),
          scope: "完整作品跨章节结构与独立样本复核",
        },
      },
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    await user.click(
      await screen.findByRole("checkbox", { name: /我已逐项确认/ }),
    );
    expect(
      screen.getByRole("button", { name: "保存正式复核" }),
    ).toBeInTheDocument();
    await user.type(screen.getByRole("textbox", { name: "标题" }), "再改");
    expect(
      screen.getByRole("checkbox", { name: /我已逐项确认/ }),
    ).not.toBeChecked();
    await user.click(screen.getByRole("button", { name: "保存复核草案" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const savedCard = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(savedCard?.draft_candidate).toBe(true);
    expect(savedCard?.requires_human_distillation).toBe(true);
  });

  it("does not fabricate missing fields, unchecked validation passes, or clamped scores", async () => {
    const user = userEvent.setup();
    const candidate: KnowledgeCandidate = {
      ...knowledgeCandidateFixture,
      card: {
        ...knowledgeCandidateFixture.card,
        summary: "",
        reader_promise: "",
        source: {
          name: "测试作品",
          scope: "",
          evidence_quality: "weak",
          notes: "",
        },
        type_payload: {},
        validation_ladder: Object.fromEntries(
          [
            "internal_consistency",
            "predictive_validation",
            "blind_sample_validation",
            "extreme_scenario_stress_test",
          ].map((step) => [step, { status: "warn", evidence: [] }]),
        ),
        scores: {
          evidence: 0.2,
          specificity: 0.25,
          generativity: 0.3,
          retrieval: 0.2,
          safety_risk: 0.8,
        },
        draft_candidate: true,
        requires_human_distillation: true,
      },
      issues: [
        {
          code: "draft_flag_present",
          field: "draft_candidate",
          message: "draft",
        },
      ],
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    await user.type(
      await screen.findByRole("textbox", { name: "标题" }),
      "作者补标题",
    );
    await user.type(screen.getByRole("textbox", { name: "摘要" }), "短摘要");
    await user.click(screen.getByRole("button", { name: "保存复核草案" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const savedCard = vi.mocked(updateKnowledgeCandidate).mock.calls[0]?.[1];
    expect(savedCard?.draft_candidate).toBe(true);
    expect(savedCard?.requires_human_distillation).toBe(true);
    expect(savedCard?.summary).toBe("短摘要");
    expect(savedCard?.reader_promise).toBe("");
    expect(savedCard?.source).toMatchObject({
      evidence_quality: "weak",
      notes: "",
      scope: "",
    });
    expect(savedCard?.type_payload).toEqual({
      premise_pattern: "",
      core_reader_promise: "",
      opening_contract: "",
      progression_map: [],
      recurring_scene_loops: [],
      transfer_limits: [],
    });
    expect(savedCard?.validation_ladder).toMatchObject({
      internal_consistency: { status: "warn", evidence: [] },
      predictive_validation: { status: "warn", evidence: [] },
      blind_sample_validation: { status: "warn", evidence: [] },
      extreme_scenario_stress_test: { status: "warn", evidence: [] },
    });
    expect(savedCard?.scores).toMatchObject({
      evidence: 0.2,
      specificity: 0.25,
      generativity: 0.3,
      retrieval: 0.2,
      safety_risk: 0.8,
    });
  });

  it("reviews voice taxonomy, all 13 voice payload fields, and the fixed L5 route", async () => {
    const user = userEvent.setup();
    const candidate = voiceCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    expect(
      await screen.findByText("反馈层与卡片的 NLCL 执行层不一致"),
    ).toBeInTheDocument();
    for (const label of [
      "平台",
      "受众",
      "题材分类",
      "子题材",
      "风格分类",
      "视角分类",
      "内容边界",
      "视角距离",
      "句式节奏",
      "用词池",
      "对话行为",
      "描写行为",
      "情绪处理",
      "自然网文表达",
      "句式变化",
      "场景锚点规则",
      "对话压力规则",
      "AI 痕迹失败特征",
      "可读性验收测试",
      "禁止复制内容",
      "NLCL 路由层",
    ]) {
      expect(screen.getByRole("textbox", { name: label })).toBeInTheDocument();
    }
    expect(screen.getByRole("textbox", { name: "反馈层" })).toHaveValue(
      "L5_DEAI_STYLE",
    );

    const style = screen.getByRole("textbox", { name: "风格分类" });
    await user.clear(style);
    await user.type(style, "现场压力\n自然网文\n人物判断");
    await user.click(screen.getByRole("checkbox", { name: /我已逐项确认/ }));
    await user.click(screen.getByRole("button", { name: "保存正式复核" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const [casCandidate, savedCard] =
      vi.mocked(updateKnowledgeCandidate).mock.calls[0] ?? [];
    expect(casCandidate).toBe(candidate);
    expect(savedCard?.draft_candidate).toBe(false);
    expect(savedCard?.requires_human_distillation).toBe(false);
    expect(savedCard?.source).toMatchObject({
      evidence_quality: "medium",
      name: "测试作品",
      notes: "跨章节结构与独立样本复核完成。",
    });
    expect(String((savedCard?.source as Record<string, unknown>)?.scope)).toBe(
      "完整作品跨章节结构与独立样本复核",
    );
    expect(savedCard?.taxonomy).toMatchObject({
      platform: ["番茄"],
      audience: ["男频"],
      genre: ["玄幻"],
      pov: ["贴近主角的有限视角"],
      content_boundary: ["只迁移抽象叙事行为"],
    });
    expect(
      (savedCard?.taxonomy as Record<string, string[]>).style.join("\n"),
    ).toContain("现场压力");
    expect(
      (savedCard?.taxonomy as Record<string, string[]>).style.join("\n"),
    ).toContain("自然网文");
    expect(
      (savedCard?.taxonomy as Record<string, string[]>).style.join("\n"),
    ).toContain("人物判断");
    expect(
      Object.keys(savedCard?.type_payload as Record<string, unknown>).sort(),
    ).toEqual([
      "anti_ai_failure_signatures",
      "description_behavior",
      "dialogue_behavior",
      "dialogue_pressure_rules",
      "diction_pool",
      "do_not_copy",
      "emotion_handling",
      "human_web_prose",
      "pov_distance",
      "readability_acceptance_tests",
      "scene_anchor_rules",
      "sentence_rhythm",
      "sentence_variety",
    ]);
    expect(savedCard?.nlcl_route_hints).toMatchObject({
      nlcl_layers: ["distillation_index", "style", "deai"],
      feedback_default_layer: "L5_DEAI_STYLE",
    });
  }, 15_000);

  it("reviews validator-specific fields and binds feedback to its selected NLCL layer", async () => {
    const user = userEvent.setup();
    const candidate = validatorCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    expect(
      await screen.findByText("校验器执行层不在受管 NLCL 层内"),
    ).toBeInTheDocument();
    expect(screen.getByText("缺少可执行的正文校验规则")).toBeInTheDocument();
    for (const label of [
      "平台",
      "受众",
      "题材分类",
      "子题材",
      "问题症状",
      "内容边界",
      "校验器名称",
      "适用对象",
      "检查项",
      "拒收条件",
      "重写路由",
      "NLCL 路由层",
    ]) {
      expect(screen.getByRole("textbox", { name: label })).toBeInTheDocument();
    }

    await user.selectOptions(
      screen.getByRole("combobox", { name: "执行层" }),
      "L4_DRAFT_EXECUTION",
    );
    await user.click(screen.getByRole("button", { name: "添加执行规则" }));
    const firstRule = screen.getByRole("group", { name: "执行规则 1" });
    const firstRuleId = within(firstRule).getByRole("textbox", {
      name: "规则 ID",
    });
    await user.clear(firstRuleId);
    await user.type(firstRuleId, "z_forbid_source_terms");
    await user.type(
      within(firstRule).getByRole("textbox", { name: "匹配词项" }),
      "  Archive Night  {enter}{enter} source shell  ",
    );
    await user.click(
      within(firstRule).getByRole("checkbox", { name: "区分大小写" }),
    );
    const firstFailureSignature = within(firstRule).getByRole("textbox", {
      name: "失败签名",
    });
    await user.clear(firstFailureSignature);
    await user.type(firstFailureSignature, "validator_source_term_leak");
    await user.type(
      within(firstRule).getByRole("textbox", { name: "失败提示" }),
      "正文候选包含正式校验器禁止的来源词项。",
    );

    await user.click(screen.getByRole("button", { name: "添加执行规则" }));
    const secondRule = screen.getByRole("group", { name: "执行规则 2" });
    const secondRuleId = within(secondRule).getByRole("textbox", {
      name: "规则 ID",
    });
    await user.clear(secondRuleId);
    await user.type(secondRuleId, "a_require_visible_pressure");
    await user.selectOptions(
      within(secondRule).getByRole("combobox", { name: "规则类型" }),
      "require_any_term",
    );
    await user.type(
      within(secondRule).getByRole("textbox", { name: "匹配词项" }),
      "  代价  {enter} 压力 ",
    );
    const secondFailureSignature = within(secondRule).getByRole("textbox", {
      name: "失败签名",
    });
    await user.clear(secondFailureSignature);
    await user.type(
      secondFailureSignature,
      "validator_visible_pressure_missing",
    );
    await user.type(
      within(secondRule).getByRole("textbox", { name: "失败提示" }),
      "正文候选缺少可见压力或代价词项。",
    );

    await user.click(screen.getByRole("button", { name: "添加执行规则" }));
    expect(screen.getByText("3 / 16")).toBeInTheDocument();
    const temporaryRule = screen.getByRole("group", { name: "执行规则 3" });
    await user.click(
      within(temporaryRule).getByRole("button", { name: "删除执行规则 3" }),
    );
    expect(
      screen.queryByRole("group", { name: "执行规则 3" }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("2 / 16")).toBeInTheDocument();
    expect(screen.getByRole("textbox", { name: "反馈层" })).toHaveValue(
      "L4_DRAFT_EXECUTION",
    );
    await user.click(screen.getByRole("checkbox", { name: /我已逐项确认/ }));
    await user.click(screen.getByRole("button", { name: "保存正式复核" }));

    await waitFor(() =>
      expect(updateKnowledgeCandidate).toHaveBeenCalledTimes(1),
    );
    const [casCandidate, savedCard] =
      vi.mocked(updateKnowledgeCandidate).mock.calls[0] ?? [];
    expect(casCandidate).toBe(candidate);
    expect(savedCard?.draft_candidate).toBe(false);
    expect(savedCard?.requires_human_distillation).toBe(false);
    expect(savedCard?.source).toMatchObject({
      evidence_quality: "medium",
      name: "测试作品",
      notes: "跨章节结构与独立样本复核完成。",
    });
    expect(String((savedCard?.source as Record<string, unknown>)?.scope)).toBe(
      "完整作品跨章节结构与独立样本复核",
    );
    expect(savedCard?.taxonomy).toMatchObject({
      platform: ["番茄"],
      audience: ["男频"],
      genre: ["玄幻"],
      problem_symptom: ["抽象总结", "来源污染"],
      content_boundary: ["正文候选拒收闸门"],
    });
    expect(savedCard?.type_payload).toEqual({
      validator_name: "character_pressure_and_source_contamination_gate",
      applies_to: ["完整章节正文候选", "针对性正文改写候选"],
      layer_id: "L4_DRAFT_EXECUTION",
      checks: [
        "人物选择是否通过动作、关系或资源变化产生代价。",
        "正文是否避开来源名称、设定壳和标志句。",
      ],
      reject_conditions: [
        "连续使用抽象心理总结替代人物行动。",
        "出现来源专名、标志表达或完整场景顺序。",
      ],
      execution_rules: [
        {
          rule_id: "a_require_visible_pressure",
          kind: "require_any_term",
          terms: ["代价", "压力"],
          case_sensitive: false,
          failure_signature: "validator_visible_pressure_missing",
          message: "正文候选缺少可见压力或代价词项。",
        },
        {
          rule_id: "z_forbid_source_terms",
          kind: "forbid_terms",
          terms: ["Archive Night", "source shell"],
          case_sensitive: true,
          failure_signature: "validator_source_term_leak",
          message: "正文候选包含正式校验器禁止的来源词项。",
        },
      ],
      rewrite_routes: ["L4_DRAFT_EXECUTION", "L5_DEAI_STYLE"],
    });
    expect(savedCard?.nlcl_route_hints).toMatchObject({
      nlcl_layers: ["distillation_index", "review"],
      feedback_default_layer: "L4_DRAFT_EXECUTION",
    });
  }, 15_000);

  it("imports a source draft only when no candidate receipt exists", async () => {
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [],
    });
    render(
      <KnowledgeReviewPanel
        sourceImportId={knowledgeCandidateFixture.source_import_id}
      />,
    );

    await waitFor(() =>
      expect(importKnowledgeCandidates).toHaveBeenCalledWith(
        knowledgeCandidateFixture.source_import_id,
      ),
    );
  });

  it("requires explicit confirmation before replacing the active formal version", async () => {
    const user = userEvent.setup();
    const candidate = readyCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    render(<KnowledgeReviewPanel />);

    const promote = await screen.findByRole("button", { name: "晋升为 v2" });
    expect(promote).toBeDisabled();
    expect(screen.getByText("需要明确替换现行版本")).toBeInTheDocument();

    await user.click(
      screen.getByRole("checkbox", {
        name: "我确认把此候选晋升为新的现行版本",
      }),
    );
    expect(promote).toBeEnabled();
    await user.click(promote);

    await waitFor(() =>
      expect(promoteKnowledgeCandidate).toHaveBeenCalledWith(candidate, true),
    );
  });

  it("promotes a new card without replacement confirmation", async () => {
    const user = userEvent.setup();
    const candidate = readyCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    render(<KnowledgeReviewPanel />);

    await user.click(await screen.findByRole("button", { name: "晋升正式卡" }));

    await waitFor(() =>
      expect(promoteKnowledgeCandidate).toHaveBeenCalledWith(candidate, false),
    );
  });

  it("offers a state reload after a promotion conflict", async () => {
    const user = userEvent.setup();
    const candidate = readyCandidate();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [],
    });
    vi.mocked(promoteKnowledgeCandidate).mockRejectedValueOnce(
      new RuntimeApiError(409, {
        error: { message: "knowledge candidate changed before promotion" },
      }),
    );
    render(<KnowledgeReviewPanel />);

    await user.click(await screen.findByRole("button", { name: "晋升正式卡" }));
    expect(
      await screen.findByText("knowledge candidate changed before promotion"),
    ).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "重新载入状态" }));

    await waitFor(() =>
      expect(listKnowledgeCandidates).toHaveBeenCalledTimes(2),
    );
  });

  it("rolls a formal card back to the selected older version", async () => {
    const user = userEvent.setup();
    const card = versionedCard();
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [card],
    });
    render(<KnowledgeReviewPanel />);

    await user.click(
      await screen.findByRole("button", {
        name: `回滚 ${card.card_id} 到 v1`,
      }),
    );

    await waitFor(() =>
      expect(rollbackKnowledgeCard).toHaveBeenCalledWith(
        card,
        knowledgeCardFixture.active_version_id,
      ),
    );
  });

  it("clears a source import candidate filter explicitly", async () => {
    const user = userEvent.setup();
    const onClearSourceImport = vi.fn();
    render(
      <KnowledgeReviewPanel
        sourceImportId={knowledgeCandidateFixture.source_import_id}
        onClearSourceImport={onClearSourceImport}
      />,
    );

    await user.click(
      await screen.findByRole("button", { name: "查看全部候选" }),
    );
    expect(onClearSourceImport).toHaveBeenCalledTimes(1);
  });

  it.each(["pending", "deferred", "promoted"] as const)(
    "rejects a %s review-stage candidate through its immutable identity",
    async (status) => {
      const user = userEvent.setup();
      const candidate: KnowledgeCandidate = {
        ...readyCandidate(),
        status,
        promoted_version_id:
          status === "promoted" ? knowledgeCardFixture.active_version_id : null,
      };
      vi.mocked(listKnowledgeCandidates).mockResolvedValue({
        ...knowledgeCandidateListFixture,
        candidates: [candidate],
      });
      render(
        <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
      );

      await user.click(await screen.findByRole("button", { name: "拒绝候选" }));

      await waitFor(() =>
        expect(rejectKnowledgeCandidate).toHaveBeenCalledWith(candidate),
      );
    },
  );

  it("finalizes a terminal verified batch and displays its immutable receipt", async () => {
    const user = userEvent.setup();
    const candidate: KnowledgeCandidate = {
      ...readyCandidate(),
      status: "promoted",
      promoted_version_id: knowledgeCardFixture.active_version_id,
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(getKnowledgeSourceFormalization)
      .mockResolvedValueOnce(knowledgeAwaitingFormalizationFixture)
      .mockResolvedValue(knowledgeFormalizationFixture);
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    await user.click(
      await screen.findByRole("button", { name: "完成本批复核" }),
    );

    await waitFor(() =>
      expect(finalizeKnowledgeSourceImport).toHaveBeenCalledWith(
        candidate.source_import_id,
        {
          expected_source_receipt_hash:
            knowledgeSourceReceiptFixture.receipt?.receipt_hash,
          expected_draft_hash:
            knowledgeSourceReceiptFixture.receipt?.cards_draft_hash,
          expected_candidate_set_hash:
            knowledgeCandidateListFixture.candidate_set_hash,
        },
      ),
    );
    expect(
      await screen.findByText(/completed · 本批复核已完成 · 三类知识已正式化/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        knowledgeFormalizationReceiptFixture.formalization_receipt_id,
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByTitle(knowledgeFormalizationReceiptFixture.receipt_hash),
    ).toBeInTheDocument();
  });

  it("offers the writing handoff only for a verified project batch", async () => {
    const user = userEvent.setup();
    const onStartWriting = vi.fn().mockResolvedValue(undefined);
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    const projectRevision = 4;
    const projectFingerprint = "7".repeat(64);
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [
        {
          ...knowledgeCandidateFixture,
          status: "promoted",
          promoted_version_id: knowledgeCardFixture.active_version_id,
        },
      ],
    });
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeFormalizationFixture,
    );
    vi.mocked(getProjectKbState).mockResolvedValue(
      currentProjectKbStateFixture({
        projectId,
        revision: projectRevision,
        fingerprint: projectFingerprint,
        knowledgeScopeStateRevision:
          knowledgeScopedCardListFixture.scope.state_revision,
        knowledgeFingerprint: knowledgeScopedCardListFixture.fingerprint,
      }),
    );

    render(
      <KnowledgeReviewPanel
        projectId={projectId}
        projectRevision={projectRevision}
        projectFingerprint={projectFingerprint}
        sourceImportId={knowledgeCandidateFixture.source_import_id}
        onStartWriting={onStartWriting}
      />,
    );

    const start = await screen.findByRole("button", {
      name: "使用这批知识开始写作",
    });
    await waitFor(() => expect(start).toBeEnabled());
    await user.click(start);
    expect(onStartWriting).toHaveBeenCalledWith({
      schema: "zhimeng.formal-knowledge-selector.v1",
      source_import_id: knowledgeCandidateFixture.source_import_id,
      formalization_receipt_id:
        knowledgeFormalizationReceiptFixture.formalization_receipt_id,
      formalization_receipt_hash:
        knowledgeFormalizationReceiptFixture.receipt_hash,
      expected_knowledge_fingerprint:
        knowledgeScopedCardListFixture.fingerprint,
      cards: [
        {
          card_id: knowledgeCardFixture.card_id,
          version_id: knowledgeCardFixture.active_version_id,
          content_hash: knowledgeCardFixture.active_version.content_hash,
        },
      ],
    });
  });

  it("keeps the writing handoff visible but disabled until the project KB is current", async () => {
    const user = userEvent.setup();
    const onStartWriting = vi.fn().mockResolvedValue(undefined);
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [
        {
          ...knowledgeCandidateFixture,
          status: "promoted",
          promoted_version_id: knowledgeCardFixture.active_version_id,
        },
      ],
    });
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeFormalizationFixture,
    );
    vi.mocked(getProjectKbState).mockResolvedValue({
      ...currentProjectKbStateFixture({
        projectId,
        revision: 4,
        fingerprint: "7".repeat(64),
      }),
      status: "missing",
      pointer: null,
      manifest: null,
    });

    render(
      <KnowledgeReviewPanel
        projectId={projectId}
        projectRevision={4}
        projectFingerprint={"7".repeat(64)}
        sourceImportId={knowledgeCandidateFixture.source_import_id}
        onStartWriting={onStartWriting}
      />,
    );

    const start = await screen.findByRole("button", {
      name: "使用这批知识开始写作",
    });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute(
      "title",
      expect.stringContaining("重建或校验"),
    );
    expect(
      screen.getByText(/状态为 current 后才能开始写作/),
    ).toBeInTheDocument();
    await user.click(start);
    expect(onStartWriting).not.toHaveBeenCalled();
  });

  it("fails closed when the formalized batch no longer matches the active card version", async () => {
    const user = userEvent.setup();
    const onStartWriting = vi.fn().mockResolvedValue(undefined);
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string;
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [
        {
          ...knowledgeCandidateFixture,
          status: "promoted",
          promoted_version_id: knowledgeCardFixture.active_version_id,
        },
      ],
    });
    vi.mocked(listProjectKnowledgeCards).mockResolvedValue({
      ...knowledgeScopedCardListFixture,
      cards: [versionedCard()],
    });
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeFormalizationFixture,
    );

    render(
      <KnowledgeReviewPanel
        projectId={projectId}
        sourceImportId={knowledgeCandidateFixture.source_import_id}
        onStartWriting={onStartWriting}
      />,
    );

    const start = await screen.findByRole("button", {
      name: "使用这批知识开始写作",
    });
    expect(start).toBeDisabled();
    expect(start).toHaveAttribute("title", "批次引用已变化，请刷新后重试");
    await user.click(start);
    expect(onStartWriting).not.toHaveBeenCalled();
  });

  it("labels legacy source batches without claiming they can be finalized", async () => {
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [{ ...knowledgeCandidateFixture, status: "rejected" }],
    });
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue({
      ...knowledgeSourceReceiptFixture,
      lineage_status: "legacy_unverified",
      receipt: null,
    });
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue({
      ...knowledgeAwaitingFormalizationFixture,
      status: "legacy_unverified",
    });
    render(
      <KnowledgeReviewPanel
        sourceImportId={knowledgeCandidateFixture.source_import_id}
      />,
    );

    expect(
      await screen.findByText("来源 legacy_unverified"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("legacy 来源没有可验证执行收据，不能完成正式化。"),
    ).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: "完成本批复核" }),
    ).not.toBeInTheDocument();
  });

  it("labels promoted versions as staged until the whole batch is formalized", async () => {
    const candidate: KnowledgeCandidate = {
      ...readyCandidate(),
      status: "promoted",
      promoted_version_id: knowledgeCardFixture.active_version_id,
    };
    const stagedCard: KnowledgeCard = {
      ...knowledgeCardFixture,
      active_version: {
        ...knowledgeCardFixture.active_version,
        formalization_status: "awaiting_formalization",
        formalization: null,
      },
      versions: [
        {
          ...knowledgeCardFixture.active_version,
          formalization_status: "awaiting_formalization",
          formalization: null,
        },
      ],
    };
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [candidate],
    });
    vi.mocked(listKnowledgeCards).mockResolvedValue({
      ...knowledgeCardListFixture,
      cards: [stagedCard],
    });
    render(
      <KnowledgeReviewPanel sourceImportId={candidate.source_import_id} />,
    );

    expect(
      (await screen.findAllByText("待整批正式化")).length,
    ).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("0 待复核 · 0 正式卡")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "停用正式卡" })).toBeDisabled();
  });
});

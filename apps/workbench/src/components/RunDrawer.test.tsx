import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentActivity, AgentRun } from '../api/agent'
import type { AgentConversationState } from '../hooks/useAgentConversation'
import {
  agentCandidateOutputFixture,
  legacyAgentCandidateOutputFixture,
} from '../test/agentCandidateOutputFixture'
import { providerRecoveryFixture } from '../test/providerRecoveryFixture'
import { providerTurnReceiptListFixture } from '../test/providerTurnReceiptFixture'
import { writerTraceFixture } from '../test/writerTraceFixture'
import { RunDrawer } from './RunDrawer'

const projectId = 'project-0123456789abcdef01234567'
const sessionId = 'session-0123456789abcdef01234567'
const runId = 'run-0123456789abcdef0123456789'
const changeSetId = 'changeset-0123456789abcdef0123456789abcdef'

function activityBase(sequence: number) {
  return {
    schema: 'zhimeng.activity-item.v1' as const,
    activity_id: `activity-${sequence.toString().padStart(8, '0')}`,
    session_id: sessionId,
    run_id: runId,
    sequence,
    created_at: '2026-07-20T05:00:00Z',
    project_revision: {
      project_id: projectId,
      revision: 3,
      fingerprint: 'a'.repeat(64),
      observed_at: '2026-07-20T05:00:00Z',
    },
    correlation_id: 'correlation-0123456789abcdef',
    causation_id: null,
  }
}

const activities: AgentActivity[] = [
  {
    ...activityBase(3),
    kind: 'tool_call',
    payload: {
      tool_call_id: 'tool-call-00000001',
      tool_name: 'project_read_file',
      arguments: {
        path: '05_正文内容/第一卷/CH001_正文.md',
        api_key: 'must-not-render',
      },
    },
  },
  {
    ...activityBase(4),
    kind: 'tool_result',
    payload: {
      tool_call_id: 'tool-call-00000001',
      status: 'succeeded',
      output: { characters: 1280 },
      error: null,
      duration_ms: 42,
    },
  },
  {
    ...activityBase(5),
    kind: 'change_proposal',
    payload: {
      proposal_id: changeSetId,
      title: '收紧第一章开场',
      summary: '保留黄铜钥匙，删除重复环境说明。',
      base_revision: 3,
      base_fingerprint: 'a'.repeat(64),
      files: [{
        file_id: 'file-0123456789abcdef0123456789abcdef',
        path: '05_正文内容/第一卷/CH001_正文.md',
        operation: 'update',
        content_hash_before: 'b'.repeat(64),
        content_hash_after: 'c'.repeat(64),
      }],
    },
  },
  {
    ...activityBase(6),
    kind: 'artifact',
    payload: {
      artifact_id: 'artifact-00000001',
      artifact_type: 'context_injection_receipt',
      name: 'Provider Context injection receipt',
      uri: 'runtime://context-packs/context-one/injection',
      media_type: 'application/json',
      content_hash: 'd'.repeat(64),
      size_bytes: 2048,
    },
  },
]

const skillActivity = {
  ...activityBase(7),
  kind: 'skill_call',
  payload: {
    skill_call_id: 'skill-call-0123456789abcdef',
    skill_id: 'novel-distillation',
    skill_version: '2.1.0',
    status: 'succeeded',
    input: {
      source_ids: ['reference-a'],
      api_key: 'must-not-render',
      access_token: 'second-secret',
    },
    output: {
      receipt: {
        schema: 'zhimeng.skill-execution-receipt.v1',
        receipt_id: `skill-receipt-${'1'.repeat(32)}`,
        run_id: runId,
        tool_call_id: 'tool-call-skill-0123456789abcdef',
        project_id: projectId,
        project_revision: 3,
        project_fingerprint: 'a'.repeat(64),
        skill_id: 'novel-distillation',
        skill_version: '2.1.0',
        skill_kind: 'deterministic_tool',
        skill_version_id: `skill-version-${'2'.repeat(32)}`,
        manifest_fingerprint: '2'.repeat(64),
        package_fingerprint: '3'.repeat(64),
        permissions: {
          read: ['bound_project_snapshot'],
          write: ['run_staging'],
          network: false,
          secrets: [],
        },
        permissions_hash: '4'.repeat(64),
        input_hash: '5'.repeat(64),
        output_hash: '6'.repeat(64),
        execution_status: 'succeeded',
        domain_status: 'blocked',
        exit_code: 1,
        started_at: '2026-07-20T05:00:01Z',
        finished_at: '2026-07-20T05:00:02Z',
        duration_ms: 864,
        artifacts: [{
          artifact_type: 'distillation-cards',
          relative_path: 'artifacts/cards.json',
          content_hash: '7'.repeat(64),
        }],
        error: null,
      },
      result_summary: '生成 3 张候选卡，领域门禁阻止自动晋升。',
    },
  },
} satisfies Extract<AgentActivity, { kind: 'skill_call' }>

const failedSkillActivity = {
  ...skillActivity,
  ...activityBase(8),
  payload: {
    ...skillActivity.payload,
    skill_call_id: 'skill-call-failed-0123456789',
    status: 'failed',
    output: {
      receipt: {
        ...skillActivity.payload.output.receipt,
        receipt_id: `skill-receipt-${'8'.repeat(32)}`,
        execution_status: 'failed',
        domain_status: null,
        exit_code: 2,
        output_hash: '8'.repeat(64),
        error: {
          code: 'skill_process_exited',
          message: '确定性进程返回非零退出码。',
          retryable: false,
        },
      },
      result_summary: null,
    },
  },
} satisfies Extract<AgentActivity, { kind: 'skill_call' }>

const activeRun: AgentRun = {
  schema: 'zhimeng.execution-run.v1',
  run_id: runId,
  session_id: sessionId,
  ordinal: 2,
  status: 'succeeded',
  objective: '收紧第一章开场',
  created_at: '2026-07-20T05:00:00Z',
  updated_at: '2026-07-20T05:00:03Z',
  started_at: '2026-07-20T05:00:00Z',
  completed_at: '2026-07-20T05:00:03Z',
  executor: {},
  capabilities: {},
  project_revision: activities[0].project_revision,
  correlation_id: 'correlation-0123456789abcdef',
  causation_id: null,
  last_activity_sequence: 6,
  checkpoint_id: null,
  error: null,
}

function conversation(): AgentConversationState {
  return {
    availability: 'available',
    detail: 'ready',
    sessions: [],
    session: null,
    messages: [],
    streamingContent: '',
    activeRun,
    workflow: null,
    activities,
    pendingContextPack: null,
    pendingNlclRoute: null,
    pendingNlclRouteExecutions: [],
    pendingPrewriteOperationPackage: null,
    activeContextPack: null,
    activeNlclRoute: null,
    activeNlclRouteExecutions: [],
    activePrewriteOperationPackage: null,
    contextState: 'idle',
    contextStaleReasons: [],
    running: false,
    resuming: false,
    switchingSession: false,
    busy: false,
    error: null,
    compileContext: vi.fn().mockResolvedValue(undefined),
    confirmPendingContext: vi.fn().mockResolvedValue(undefined),
    discardPendingContext: vi.fn(),
    refreshPendingContext: vi.fn().mockResolvedValue(undefined),
  openContextPackForReview: vi.fn().mockResolvedValue(undefined),
    cancel: vi.fn().mockResolvedValue(undefined),
    resume: vi.fn().mockResolvedValue(undefined),
    createConversation: vi.fn().mockResolvedValue(undefined),
    selectConversation: vi.fn().mockResolvedValue(undefined),
    refresh: vi.fn(),
  }
}

describe('RunDrawer', () => {
  afterEach(cleanup)

  it('renders a strict candidate envelope with policy, provenance, and references', async () => {
    const user = userEvent.setup()
    const candidate = await agentCandidateOutputFixture()
    const providerEvidence = await providerTurnReceiptListFixture()
    const refresh = vi.fn()
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh }}
      providerTurnReceipts={{ evidence: providerEvidence, state: 'ready', error: null, refresh: vi.fn() }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('写前修复方案')).toBeInTheDocument()
    expect(screen.getByText(/Agent 候选 · 修复方案 · kind=repair_plan/)).toBeInTheDocument()
    expect(screen.getByText('正式正文未授权')).toBeInTheDocument()
    expect(screen.getByText(/formal_draft_allowed=false · requires_author_review=true · allowed_output=repair_plan_or_risk_draft_only/)).toBeInTheDocument()
    expect(screen.getByText(candidate.run_id)).toBeInTheDocument()
    expect(screen.getByText(candidate.provider_boundary_id)).toBeInTheDocument()
    expect(screen.getByText('verified · 1 turn')).toBeInTheDocument()
    if (candidate.schema !== 'zhimeng.agent-candidate-output.v2') {
      throw new Error('expected current Candidate v2')
    }
    expect(screen.getByText(candidate.provider_turn_receipts[0]!.receipt_id)).toBeInTheDocument()
    expect(screen.getByText('playwright-scripted-model')).toBeInTheDocument()
    expect(screen.getByText(/turn 1 · exact_match/)).toBeInTheDocument()
    expect(screen.getByText(/playwright-scripted-model → playwright-scripted-model/)).toBeInTheDocument()
    expect(screen.getByText('prewrite_gate_pressure_missing')).toBeInTheDocument()
    expect(screen.getAllByText('context-pack-candidate-fixture')).toHaveLength(2)
    expect(screen.getByText('将周鹤亭的压力放入开场场景。')).toBeInTheDocument()
    expect(screen.getByText(candidate.envelope_hash)).toBeInTheDocument()
    expect(refresh).not.toHaveBeenCalled()
  })

  it('marks historical v1 candidates as unverified without fabricated Provider receipts', async () => {
    const user = userEvent.setup()
    const candidate = await legacyAgentCandidateOutputFixture()
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('legacy_unverified · v1 历史记录')).toBeInTheDocument()
    expect(screen.getByText(/legacy_unverified：历史 v1 候选没有 Provider 响应身份收据/)).toBeInTheDocument()
    expect(screen.queryByText(/Provider 回合收据/)).not.toBeInTheDocument()
  })

  it.each([
    {
      label: 'accepted alias',
      options: {
        requestedModel: 'requested-model',
        reportedModel: 'gateway-model',
        acceptedReportedModels: ['gateway-model'],
      },
      status: 'accepted_alias',
      renderedModels: 'requested-model → gateway-model · tool_calls',
      aggregate: 'verified · 1 turn',
    },
    {
      label: 'unreported model',
      options: { reportedModel: null },
      status: 'unreported',
      renderedModels: 'playwright-scripted-model → 未报告 · tool_calls',
      aggregate: 'unreported · 1 turn',
    },
  ] as const)('renders requested and reported Provider identity for $label', async ({
    options,
    status,
    renderedModels,
    aggregate,
  }) => {
    const user = userEvent.setup()
    const candidate = await agentCandidateOutputFixture(options)
    const evidence = await providerTurnReceiptListFixture(options)
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
      providerTurnReceipts={{ evidence, state: 'ready', error: null, refresh: vi.fn() }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText(aggregate)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(`turn 1 · ${status}`))).toBeInTheDocument()
    expect(screen.getByText(new RegExp(renderedModels))).toBeInTheDocument()
  })

  it('shows a fail-closed identity state when full Receipt cross-validation fails', async () => {
    const user = userEvent.setup()
    const candidate = await agentCandidateOutputFixture()
    const refresh = vi.fn()
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
      providerTurnReceipts={{ evidence: null, state: 'error', error: 'Provider Receipt List 未绑定同一 Candidate', refresh }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('invalid_evidence · 未通过交叉校验')).toBeInTheDocument()
    expect(screen.getByRole('alert')).toHaveTextContent('未绑定同一 Candidate')
    await user.click(screen.getByRole('button', { name: '重试' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('shows the Writer task and result as the authority for a review-only prose ChangeSet', async () => {
    const user = userEvent.setup()
    const trace = await writerTraceFixture('author_review_change_proposal')
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: trace.candidate, state: 'ready', error: null, refresh: vi.fn() }}
      writerTrace={{
        workflow: trace.workflow,
        task: trace.task,
        activation: trace.activation,
        materialization: trace.materialization,
        dispatch: trace.dispatch,
        candidate: trace.candidate,
        result: trace.result,
        recovery: null,
        state: 'ready',
        error: null,
        refresh: vi.fn(),
      }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByRole('article', { name: 'Writer 任务与候选结果' })).toHaveTextContent('正文候选任务')
    expect(screen.getByText('只创建供作者审阅的 ChangeSet，不直接写入项目真值。')).toBeInTheDocument()
    expect(screen.getAllByText(trace.task.writer_task_id)).toHaveLength(3)
    expect(screen.getByText(trace.result.writer_candidate_result_id)).toBeInTheDocument()
    expect(screen.getByText(/3\/3 steps/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(trace.activation.activation_binding_id))).toBeInTheDocument()
    expect(screen.getAllByText(trace.materialization.materialization_id)).toHaveLength(3)
    expect(screen.getAllByText(trace.dispatch.dispatch_receipt_id)).toHaveLength(2)
    expect(screen.getByText(/committed_before_dispatch · replay 0/)).toBeInTheDocument()
    expect(screen.getByText(/正文候选尚未写回/)).toBeInTheDocument()
    expect(screen.getByText(/writes_project_truth=false/)).toBeInTheDocument()
    expect(screen.getByText(trace.task.target_files[0]!.relative_path)).toBeInTheDocument()
  })

  it('shows a neutral empty state when the Run has no persisted candidate', async () => {
    const user = userEvent.setup()
    const refresh = vi.fn()
    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      candidateOutput={{ output: null, state: 'missing', error: null, refresh }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('当前 Run 尚无候选输出')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新检查' }))
    expect(refresh).toHaveBeenCalledTimes(1)
  })

  it('renders Provider recovery facts and keeps abandon as an explicit CAS decision', async () => {
    const user = userEvent.setup()
    const candidate = await agentCandidateOutputFixture()
    const response = await providerRecoveryFixture()
    const decide = vi.fn().mockResolvedValue(true)
    const state = conversation()

    render(<RunDrawer
      demo={false}
      conversation={state}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
      providerRecovery={{
        response,
        state: 'ready',
        action: null,
        error: null,
        actionError: null,
        canAdopt: true,
        refresh: vi.fn(),
        decide,
      }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('Provider 结果待人工决策')).toBeInTheDocument()
    expect(screen.getByText('unknown')).toBeInTheDocument()
    expect(screen.getByText('persisted')).toBeInTheDocument()
    expect(screen.getByText('provider_replay_count=0')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /采纳已保存候选/ })).toBeEnabled()

    await user.click(screen.getByRole('button', { name: /放弃未知结果/ }))
    expect(decide).toHaveBeenCalledWith('abandon_and_recompile')
    expect(state.refresh).toHaveBeenCalledTimes(1)
    expect(state.compileContext).not.toHaveBeenCalled()
    expect(state.resume).not.toHaveBeenCalled()
  })

  it('removes both recovery operations after a terminal decision', async () => {
    const user = userEvent.setup()
    const response = await providerRecoveryFixture({ decisionAction: 'abandon_and_recompile' })
    const state = conversation()

    render(<RunDrawer
      demo={false}
      conversation={state}
      providerRecovery={{
        response,
        state: 'ready',
        action: null,
        error: null,
        actionError: '恢复操作已经完成，已重新载入最新状态',
        canAdopt: false,
        refresh: vi.fn(),
        decide: vi.fn().mockResolvedValue(false),
      }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getAllByText('已放弃未知结果')).toHaveLength(2)
    expect(screen.getByText(/Run 结果：failed/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /采纳已保存候选/ })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /放弃未知结果/ })).not.toBeInTheDocument()
    expect(state.compileContext).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '重新编译新 Run' }))
    expect(state.compileContext).toHaveBeenCalledWith(state.activeRun?.objective)
    expect(screen.getByRole('alert')).toHaveTextContent('恢复操作已经完成')
  })

  it('keeps adoption disabled when the recovery dossier has no persisted candidate', async () => {
    const user = userEvent.setup()
    const response = await providerRecoveryFixture({ candidateState: 'absent' })

    render(<RunDrawer
      demo={false}
      conversation={conversation()}
      providerRecovery={{
        response,
        state: 'ready',
        action: null,
        error: null,
        actionError: null,
        canAdopt: false,
        refresh: vi.fn(),
        decide: vi.fn().mockResolvedValue(false),
      }}
    />)

    await user.click(screen.getByRole('tab', { name: /结果/ }))

    expect(screen.getByText('没有候选')).toBeInTheDocument()
    expect(screen.getByText('absent')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /采纳已保存候选/ })).toBeDisabled()
    expect(screen.getByRole('button', { name: /放弃未知结果/ })).toBeEnabled()
  })

  it('pairs real ToolCall and ToolResult activities and redacts secrets', async () => {
    const user = userEvent.setup()
    render(<RunDrawer demo={false} conversation={conversation()} />)

    await user.click(screen.getByRole('tab', { name: /工具/ }))

    expect(screen.getByText('project_read_file')).toBeInTheDocument()
    expect(screen.getByText('只读 · 当前项目')).toBeInTheDocument()
    expect(screen.getByText('执行成功')).toBeInTheDocument()
    expect(screen.getByText(/05_正文内容/)).toBeInTheDocument()
    expect(screen.getByText(/"characters": 1280/)).toBeInTheDocument()
    expect(screen.getByText(/已隐藏/)).toBeInTheDocument()
    expect(screen.queryByText(/must-not-render/)).not.toBeInTheDocument()
  })

  it('describes proposal staging conservatively and does not infer unknown tool scope', async () => {
    const user = userEvent.setup()
    const scopedActivities: AgentActivity[] = [{
      ...activityBase(7),
      kind: 'tool_call',
      payload: {
        tool_call_id: 'tool-call-00000002',
        tool_name: 'change_set_propose',
        arguments: {},
      },
    }, {
      ...activityBase(8),
      kind: 'tool_call',
      payload: {
        tool_call_id: 'tool-call-00000003',
        tool_name: 'unknown_tool',
        arguments: {},
      },
    }]
    const state = conversation()
    render(<RunDrawer demo={false} conversation={{ ...state, activities: scopedActivities }} />)

    await user.click(screen.getByRole('tab', { name: /工具/ }))

    expect(screen.getByText('候选暂存 · 不写正式项目')).toBeInTheDocument()
    expect(screen.getByText('未声明')).toBeInTheDocument()
  })

  it('opens the canonical ChangeSet and renders persisted artifacts', async () => {
    const user = userEvent.setup()
    const onReviewChangeSet = vi.fn()
    render(
      <RunDrawer
        demo={false}
        conversation={conversation()}
        onReviewChangeSet={onReviewChangeSet}
      />,
    )

    await user.click(screen.getByRole('tab', { name: /过程/ }))
    await user.click(screen.getByRole('button', { name: '审阅改动' }))
    expect(onReviewChangeSet).toHaveBeenCalledWith(changeSetId)

    await user.click(screen.getByRole('tab', { name: /文件/ }))
    expect(screen.getByText('Provider Context injection receipt')).toBeInTheDocument()
    expect(screen.getByText(/context_injection_receipt/)).toBeInTheDocument()
    expect(screen.getByText(/2.0 KB/)).toBeInTheDocument()
  })

  it('renders a deterministic Skill receipt with provenance and redacted inputs', async () => {
    const user = userEvent.setup()
    const state = conversation()
    render(
      <RunDrawer
        demo={false}
        conversation={{ ...state, activities: [skillActivity] }}
      />,
    )

    await user.click(screen.getByRole('tab', { name: /技能/ }))

    expect(screen.getByText('确定性 Skill · deterministic_tool')).toBeInTheDocument()
    expect(screen.getAllByText('novel-distillation')).toHaveLength(2)
    expect(screen.getByText(`skill-version-${'2'.repeat(32)}`)).toBeInTheDocument()
    expect(screen.getByText('2.1.0')).toBeInTheDocument()
    expect(screen.getByText(`${projectId} · revision 3`)).toBeInTheDocument()
    expect(screen.getByText('a'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('2'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('3'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('4'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('5'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('6'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText(/864 ms/)).toBeInTheDocument()
    expect(screen.getByText(/bound_project_snapshot/)).toBeInTheDocument()
    expect(screen.getByText(/distillation-cards/)).toBeInTheDocument()
    expect(screen.getByText('生成 3 张候选卡，领域门禁阻止自动晋升。')).toBeInTheDocument()
    expect(screen.getAllByText(/已隐藏/).length).toBeGreaterThanOrEqual(2)
    expect(screen.queryByText(/must-not-render|second-secret/)).not.toBeInTheDocument()
  })

  it('keeps execution and domain outcomes separate in the timeline', async () => {
    const user = userEvent.setup()
    const state = conversation()
    render(
      <RunDrawer
        demo={false}
        conversation={{ ...state, activities: [skillActivity] }}
      />,
    )

    await user.click(screen.getByRole('tab', { name: /过程/ }))

    expect(screen.getByText('确定性 Skill')).toBeInTheDocument()
    expect(screen.getByText('novel-distillation · 执行 succeeded · 领域 blocked')).toBeInTheDocument()
  })

  it('shows structured deterministic Skill failures without inventing a domain result', async () => {
    const user = userEvent.setup()
    const state = conversation()
    render(
      <RunDrawer
        demo={false}
        conversation={{ ...state, activities: [failedSkillActivity] }}
      />,
    )

    await user.click(screen.getByRole('tab', { name: /技能/ }))

    expect(screen.getAllByText('执行失败')).toHaveLength(2)
    expect(screen.getByText(/未评估/)).toBeInTheDocument()
    expect(screen.getByText('skill_process_exited')).toBeInTheDocument()
    expect(screen.getByText('确定性进程返回非零退出码。')).toBeInTheDocument()
    expect(screen.getByText('不可重试')).toBeInTheDocument()
  })
})

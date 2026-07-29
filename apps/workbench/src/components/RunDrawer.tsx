import { useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  BadgeCheck,
  Box,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  CircleDot,
  FileArchive,
  FileDiff,
  FileText,
  Hammer,
  Link2,
  RefreshCw,
  ShieldAlert,
  TerminalSquare,
  Timeline,
} from 'lucide-react'
import type {
  AgentActivity,
  AgentCandidateOutput,
  AgentRunStatus,
} from '../api/agent'
import { timelineEvents } from '../data/workbenchFixture'
import type { AgentConversationState } from '../hooks/useAgentConversation'
import type {
  AgentCandidateOutputSnapshot,
} from '../hooks/useAgentCandidateOutput'
import type { ProviderRecoverySnapshot } from '../hooks/useProviderRecovery'
import type { ProviderTurnReceiptsSnapshot } from '../hooks/useProviderTurnReceipts'
import type { WriterTraceSnapshot } from '../hooks/useWriterTrace'
import { SafeMarkdown } from './SafeMarkdown'
import { ProviderRecoveryPanel } from './ProviderRecoveryPanel'

type DrawerTab = 'timeline' | 'tools' | 'skills' | 'candidate' | 'artifacts' | 'terminal'
type ToolCallActivity = Extract<AgentActivity, { kind: 'tool_call' }>
type ToolResultActivity = Extract<AgentActivity, { kind: 'tool_result' }>
type SkillCallActivity = Extract<AgentActivity, { kind: 'skill_call' }>

interface ToolActivityPair {
  toolCallId: string
  call: ToolCallActivity | null
  result: ToolResultActivity | null
  sequence: number
}

const tabs: Array<{ id: DrawerTab; label: string; icon: React.ReactNode }> = [
  { id: 'timeline', label: '过程', icon: <Timeline size={14} /> },
  { id: 'tools', label: '工具', icon: <Hammer size={14} /> },
  { id: 'skills', label: '技能', icon: <BadgeCheck size={14} /> },
  { id: 'candidate', label: '结果', icon: <FileText size={14} /> },
  { id: 'artifacts', label: '文件', icon: <FileArchive size={14} /> },
  { id: 'terminal', label: '命令', icon: <TerminalSquare size={14} /> },
]

interface RunDrawerProps {
  demo: boolean
  conversation: AgentConversationState
  onReviewChangeSet?: (changeSetId: string) => void
  candidateOutput?: AgentCandidateOutputSnapshot
  providerTurnReceipts?: ProviderTurnReceiptsSnapshot
  writerTrace?: WriterTraceSnapshot
  providerRecovery?: ProviderRecoverySnapshot
  candidateReviewRequestKey?: number
}

const emptyCandidateOutput: AgentCandidateOutputSnapshot = {
  output: null,
  state: 'idle',
  error: null,
  refresh: () => undefined,
}

const emptyWriterTrace: WriterTraceSnapshot = {
  workflow: null,
  task: null,
  activation: null,
  materialization: null,
  dispatch: null,
  candidate: null,
  result: null,
  recovery: null,
  state: 'idle',
  error: null,
  refresh: () => undefined,
}

const emptyProviderTurnReceipts: ProviderTurnReceiptsSnapshot = {
  evidence: null,
  state: 'idle',
  error: null,
  refresh: () => undefined,
}

const activityLabels: Record<AgentActivity['kind'], string> = {
  message: '消息',
  run_state: '运行状态',
  reasoning: '推理摘要',
  tool_call: '工具调用',
  tool_result: '工具结果',
  command: '命令',
  change_proposal: '改动提案',
  approval_request: '等待审批',
  skill_call: '确定性 Skill',
  subagent: 'Subagent',
  compaction: '上下文压缩',
  artifact: '运行产物',
  error: '运行错误',
}

const runStatusLabels: Record<AgentRunStatus, string> = {
  queued: '排队中',
  starting: '启动中',
  running: '运行中',
  awaiting_approval: '等待审批',
  paused: '已暂停',
  cancelling: '取消中',
  succeeded: '已完成',
  failed: '失败',
  cancelled: '已取消',
  interrupted: '已中断',
}

const secretKey = /(authorization|api[_-]?key|password|secret|token)/i

function formatJson(value: unknown): string {
  const serialized = JSON.stringify(
    value,
    (key, current) => secretKey.test(key) ? '[已隐藏]' : current,
    2,
  ) ?? String(value)
  return serialized.length > 6000
    ? `${serialized.slice(0, 6000)}\n...（内容已截断）`
    : serialized
}

function compactId(value: string): string {
  return value.length > 28 ? `${value.slice(0, 14)}...${value.slice(-8)}` : value
}

function compactHash(value: string | null): string {
  if (!value) return '无 Hash'
  return value.length > 16 ? `${value.slice(0, 12)}...` : value
}

function skillExecutionLabel(status: SkillCallActivity['payload']['status']): string {
  if (status === 'succeeded') return '执行成功'
  if (status === 'failed') return '执行失败'
  if (status === 'cancelled') return '已取消'
  if (status === 'running') return '执行中'
  return '等待执行'
}

function skillDomainLabel(
  status: NonNullable<SkillCallActivity['payload']['output']>['receipt']['domain_status'],
): string {
  if (status === 'healthy') return '健康'
  if (status === 'degraded') return '降级'
  if (status === 'blocked') return '阻塞'
  return '未评估'
}

function toolScopeLabel(toolName: string | null): string {
  if (
    toolName === 'project_list_files'
    || toolName === 'project_read_file'
    || toolName === 'project_search'
  ) {
    return '只读 · 当前项目'
  }
  if (toolName === 'change_set_propose') {
    return '候选暂存 · 不写正式项目'
  }
  return '未声明'
}

function formatBytes(value: number | null): string {
  if (value === null) return '大小未知'
  if (value < 1024) return `${value} B`
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`
  return `${(value / (1024 * 1024)).toFixed(1)} MB`
}

function recordText(payload: Record<string, unknown>, key: string): string | null {
  const value = payload[key]
  return typeof value === 'string' && value.trim() ? value : null
}

function activityDetail(activity: AgentActivity): string {
  switch (activity.kind) {
    case 'message':
      return activity.payload.phase === 'delta'
        ? `${activity.payload.role} 正在输出`
        : `${activity.payload.role} 消息已保存`
    case 'run_state':
      return `${runStatusLabels[activity.payload.status]}${activity.payload.detail ? ` · ${activity.payload.detail}` : ''}`
    case 'tool_call':
      return `${activity.payload.tool_name} · ${compactId(activity.payload.tool_call_id)}`
    case 'tool_result':
      return `${activity.payload.status}${activity.payload.duration_ms === null ? '' : ` · ${activity.payload.duration_ms} ms`}`
    case 'change_proposal':
      return `${activity.payload.title} · ${activity.payload.files.length} 个文件`
    case 'approval_request':
      return `${activity.payload.prompt} · ${activity.payload.status}`
    case 'artifact':
      return `${activity.payload.name} · ${activity.payload.artifact_type}`
    case 'command':
      return `${activity.payload.status} · ${activity.payload.command}`
    case 'error':
      return `${activity.payload.error.error.code} · ${activity.payload.error.error.message}`
    case 'reasoning':
      return recordText(activity.payload, 'summary') ?? '推理摘要已记录'
    case 'skill_call':
      return `${activity.payload.skill_id} · 执行 ${activity.payload.output?.receipt.execution_status ?? activity.payload.status} · 领域 ${activity.payload.output?.receipt.domain_status ?? '未评估'}`
    case 'subagent':
      return recordText(activity.payload, 'name') ?? 'Subagent 活动已记录'
    case 'compaction':
      return recordText(activity.payload, 'summary') ?? '上下文压缩已记录'
  }
}

function activityIcon(activity: AgentActivity) {
  if (activity.kind === 'error') return <AlertTriangle size={13} />
  if (activity.kind === 'change_proposal') return <FileDiff size={13} />
  if (activity.kind === 'artifact') return <FileArchive size={13} />
  if (activity.kind === 'skill_call') return <BadgeCheck size={13} />
  if (activity.kind === 'tool_call' || activity.kind === 'tool_result') return <Hammer size={13} />
  if (activity.kind === 'run_state' && activity.payload.status === 'succeeded') {
    return <CheckCircle2 size={13} />
  }
  if (activity.kind === 'run_state') return <CircleDot size={13} />
  return <Box size={12} />
}

function pairToolActivities(activities: AgentActivity[]): ToolActivityPair[] {
  const pairs = new Map<string, ToolActivityPair>()
  for (const activity of activities) {
    if (activity.kind !== 'tool_call' && activity.kind !== 'tool_result') continue
    const toolCallId = activity.payload.tool_call_id
    const current = pairs.get(toolCallId) ?? {
      toolCallId,
      call: null,
      result: null,
      sequence: activity.sequence,
    }
    current.sequence = Math.min(current.sequence, activity.sequence)
    if (activity.kind === 'tool_call') current.call = activity
    if (activity.kind === 'tool_result') current.result = activity
    pairs.set(toolCallId, current)
  }
  return [...pairs.values()].sort((left, right) => left.sequence - right.sequence)
}

function ToolActivityCard({ pair }: { pair: ToolActivityPair }) {
  const status = pair.result?.payload.status ?? 'running'
  const statusLabel = status === 'succeeded'
    ? '执行成功'
    : status === 'failed'
      ? '执行失败'
      : status === 'cancelled'
        ? '已取消'
        : '执行中'
  return (
    <article className={`tool-activity-card tool-${status}`}>
      <header>
        <Hammer size={15} />
        <div><strong>{pair.call?.payload.tool_name ?? '未知工具'}</strong><span>{compactId(pair.toolCallId)}</span><span className="tool-scope">{toolScopeLabel(pair.call?.payload.tool_name ?? null)}</span></div>
        <span className="tool-status">{statusLabel}</span>
      </header>
      {pair.call && <details><summary>参数</summary><pre><code>{formatJson(pair.call.payload.arguments)}</code></pre></details>}
      {pair.result && <details open={pair.result.payload.status === 'failed'}><summary>结果{pair.result.payload.duration_ms === null ? '' : ` · ${pair.result.payload.duration_ms} ms`}</summary><pre><code>{formatJson(pair.result.payload.output)}</code></pre></details>}
      {pair.result?.payload.error && <div className="tool-error" role="alert"><AlertTriangle size={13} /><span>{pair.result.payload.error.error.message}</span></div>}
    </article>
  )
}

function SkillActivityCard({ activity }: { activity: SkillCallActivity }) {
  const receipt = activity.payload.output?.receipt ?? null
  const executionStatus = receipt?.execution_status ?? activity.payload.status
  const domainStatus = receipt?.domain_status ?? null
  const resultSummary = activity.payload.output?.result_summary ?? null

  return (
    <article className={`skill-activity-card skill-${executionStatus}`}>
      <header>
        <BadgeCheck size={16} />
        <div>
          <span className="skill-kind">确定性 Skill · {receipt?.skill_kind ?? 'deterministic_tool'}</span>
          <strong>{activity.payload.skill_id}</strong>
          <span>{compactId(activity.payload.skill_call_id)}</span>
        </div>
        <span className="skill-status">{skillExecutionLabel(executionStatus)}</span>
      </header>

      <div className="skill-status-grid">
        <div><span>执行状态</span><strong>{skillExecutionLabel(executionStatus)} <code>{executionStatus}</code></strong></div>
        <div><span>领域状态</span><strong>{skillDomainLabel(domainStatus)} <code>{domainStatus ?? 'null'}</code></strong></div>
        <div><span>耗时</span><strong>{receipt ? `${receipt.duration_ms} ms` : '等待收据'}</strong></div>
        <div><span>退出码</span><strong>{receipt?.exit_code ?? '无'}</strong></div>
      </div>

      <div className="skill-receipt-grid">
        <div><span>受管版本</span><code>{receipt?.skill_version_id ?? '未绑定'}</code></div>
        <div><span>Skill 版本</span><code>{receipt?.skill_version ?? activity.payload.skill_version ?? '未声明'}</code></div>
        <div><span>Skill kind</span><code>{receipt?.skill_kind ?? 'deterministic_tool'}</code></div>
        <div><span>Skill ID</span><code>{receipt?.skill_id ?? activity.payload.skill_id}</code></div>
        <div><span>项目快照</span><code>{receipt ? `${receipt.project_id} · revision ${receipt.project_revision}` : `${activity.project_revision.project_id} · revision ${activity.project_revision.revision}`}</code></div>
        <div><span>Project fingerprint</span><code>{receipt?.project_fingerprint ?? activity.project_revision.fingerprint}</code></div>
        <div><span>Manifest fingerprint</span><code>{receipt?.manifest_fingerprint ?? '等待收据'}</code></div>
        <div><span>Package fingerprint</span><code>{receipt?.package_fingerprint ?? '等待收据'}</code></div>
        <div><span>Permissions hash</span><code>{receipt?.permissions_hash ?? '等待收据'}</code></div>
        <div><span>Input hash</span><code>{receipt?.input_hash ?? '等待收据'}</code></div>
        <div><span>Output hash</span><code>{receipt?.output_hash ?? '无'}</code></div>
        <div><span>Receipt</span><code>{receipt ? `${receipt.schema} · ${receipt.receipt_id}` : '等待收据'}</code></div>
        <div><span>关联 Run</span><code>{receipt?.run_id ?? activity.run_id}</code></div>
        <div><span>关联 ToolCall</span><code>{receipt?.tool_call_id ?? '等待收据'}</code></div>
      </div>

      <details open>
        <summary>权限快照{receipt ? ` · ${compactHash(receipt.permissions_hash)}` : ''}</summary>
        <pre><code>{formatJson(receipt?.permissions ?? null)}</code></pre>
      </details>
      <details>
        <summary>输入</summary>
        <pre><code>{formatJson(activity.payload.input)}</code></pre>
      </details>
      {resultSummary && <div className="skill-result-summary"><span>结果摘要</span><p>{resultSummary}</p></div>}
      {receipt && receipt.artifacts.length > 0 && <details><summary>产物 · {receipt.artifacts.length}</summary><pre><code>{formatJson(receipt.artifacts)}</code></pre></details>}
      {receipt?.error && <div className="skill-error" role="alert"><AlertTriangle size={14} /><div><strong>{receipt.error.code}</strong><span>{receipt.error.message}</span><small>{receipt.error.retryable ? '可重试' : '不可重试'}</small></div></div>}
    </article>
  )
}

function candidateKindLabel(kind: AgentCandidateOutput['kind']): string {
  if (kind === 'repair_plan') return '修复方案'
  if (kind === 'risk_draft') return '风险草稿'
  if (kind === 'change_proposal') return '修改建议'
  return '下一步建议'
}

function candidateReferenceLabel(kind: AgentCandidateOutput['references'][number]['reference_kind']): string {
  if (kind === 'workflow_checkpoint') return 'Workflow Checkpoint'
  if (kind === 'runtime_identity') return 'Runtime 身份'
  if (kind === 'artifact') return '运行产物'
  return 'ChangeSet'
}

function WriterTracePanel({ snapshot }: { snapshot: WriterTraceSnapshot }) {
  if (snapshot.state === 'idle' || snapshot.state === 'missing') return null
  if (snapshot.state === 'loading') {
    return <div className="pending-run writer-trace-state"><FileText size={15} /><div><strong>正在读取 Writer 来源链</strong><span>校验任务、候选结果与项目快照</span></div></div>
  }
  if (snapshot.state === 'error') {
    return <div className="pending-run writer-trace-state writer-trace-error" role="alert"><AlertTriangle size={15} /><div><strong>Writer 来源链无效</strong><span>{snapshot.error ?? '任务与候选结果绑定失败'}</span><button type="button" className="quiet-button" onClick={snapshot.refresh}><RefreshCw size={13} />重试</button></div></div>
  }
  const workflow = snapshot.workflow
  const task = snapshot.task
  const activation = snapshot.activation
  const materialization = snapshot.materialization
  const dispatch = snapshot.dispatch
  const candidate = snapshot.candidate
  const result = snapshot.result
  if (!workflow) return null
  const authorReview = task?.mode === 'author_review_change_proposal'
  return <article className="writer-trace-card" aria-label="Writer 任务与候选结果">
    <header><FileDiff size={16} /><div><strong>{task ? (authorReview ? '正文候选任务' : '诊断修复任务') : 'Writer 来源链'}</strong><span>{task?.task_goal ?? 'Runtime 已冻结 Workflow，等待 Writer Task。'}</span></div><b>{result ? (snapshot.state === 'ready' ? '来源链已绑定' : '待交叉校验') : '链路未完成'}</b></header>
    <div className="writer-trace-policy"><ShieldAlert size={14} /><span>{authorReview ? '只创建供作者审阅的 ChangeSet，不直接写入项目真值。' : '只允许修复方案，不创建正文 ChangeSet。'}</span></div>
    <dl>
      <div><dt>受管 Workflow</dt><dd><code>{workflow.checkpoints.length}/{workflow.plan.steps.length} steps · {workflow.plan.workflow_plan_id}</code></dd></div>
      <div><dt>最终 Checkpoint</dt><dd><code>{workflow.last_checkpoint_id ?? '尚未形成'}</code></dd></div>
      {task && <><div><dt>Writer Task</dt><dd><code>{task.writer_task_id}</code></dd></div>
      <div><dt>模式</dt><dd><code>{task.mode}</code></dd></div>
      <div><dt>知识</dt><dd><code>{task.knowledge_fingerprint ?? '未绑定'} · {task.selected_card_ids.length} cards</code></dd></div>
      <div><dt>正文目标</dt><dd><code>{task.target_files.length} files · {compactHash(task.target_files_hash)}</code></dd></div>
      <div><dt>项目快照</dt><dd><code>{task.project_id} · revision {task.project_revision}</code></dd></div>
      <div><dt>Task hash</dt><dd><code>{task.task_hash}</code></dd></div></>}
      {activation && <><div><dt>指令激活</dt><dd><code>{activation.effective_scope} · {activation.skill_version} · {activation.activation_binding_id}</code></dd></div><div><dt>受管包</dt><dd><code>{compactHash(activation.package_fingerprint)} · map {compactHash(activation.instruction_map_hash)}</code></dd></div></>}
      {materialization && <><div><dt>指令物化</dt><dd><code>{materialization.materialization_id}</code></dd></div><div><dt>Provider 指令</dt><dd><code>{materialization.included_block_ids.length}/{materialization.blocks.length} blocks · {materialization.provider_instruction_utf8_bytes} bytes · {compactHash(materialization.provider_instruction_hash)}</code></dd></div></>}
      {dispatch && <><div><dt>调度收据</dt><dd><code>{dispatch.dispatch_receipt_id}</code></dd></div><div><dt>Provider Dispatch</dt><dd><code>{dispatch.status} · replay {dispatch.automatic_provider_replay_count} · {dispatch.provider_boundary_id}</code></dd></div></>}
      {candidate && <><div><dt>Candidate Envelope</dt><dd><code>{candidate.schema} · {candidate.candidate_output_id}</code></dd></div><div><dt>Candidate hash</dt><dd><code>{candidate.envelope_hash}</code></dd></div></>}
      {result && <><div><dt>候选结果</dt><dd><code>{result.writer_candidate_result_id}</code></dd></div><div><dt>ChangeSet</dt><dd><code>{result.change_set_id ?? '无'}{result.change_set_hash ? ` · ${compactHash(result.change_set_hash)}` : ''}</code></dd></div><div><dt>Result hash</dt><dd><code>{result.result_hash}</code></dd></div></>}
    </dl>
    {task && task.target_files.length > 0 && <details><summary>正文目标文件 · {task.target_files.length}</summary><ul>{task.target_files.map((target) => <li key={target.file_id}><code>{target.relative_path}</code><small title={target.content_hash}>{compactHash(target.content_hash)}</small></li>)}</ul></details>}
    {snapshot.state === 'partial' && <div className="writer-trace-partial" role="status"><AlertTriangle size={13} /><span>来源链尚未完整形成；只显示 Runtime 已持久化并通过绑定校验的证据，不推定 Provider 成功。</span></div>}
  </article>
}

function CandidateOutputPanel({
  snapshot,
  providerTurnReceipts,
  writerTrace,
}: {
  snapshot: AgentCandidateOutputSnapshot
  providerTurnReceipts: ProviderTurnReceiptsSnapshot
  writerTrace: WriterTraceSnapshot
}) {
  const candidate = snapshot.output
  if (snapshot.state === 'loading') {
    return <div className="pending-run candidate-output-empty"><FileText size={15} /><div><strong>正在读取候选输出</strong><span>正在校验 Runtime 绑定与完整性</span></div></div>
  }
  if (snapshot.state === 'error') {
    return <div className="pending-run candidate-output-empty" role="alert"><AlertTriangle size={15} /><div><strong>候选输出读取失败</strong><span>{snapshot.error ?? '读取候选输出时发生错误'}</span><button type="button" className="quiet-button" onClick={snapshot.refresh}><RefreshCw size={13} />重试</button></div></div>
  }
  if (snapshot.state === 'missing') {
    return <div className="pending-run candidate-output-empty"><FileText size={15} /><div><strong>当前 Run 尚无候选输出</strong><span>Runtime 没有已持久化的候选 Envelope。</span><button type="button" className="quiet-button" onClick={snapshot.refresh}><RefreshCw size={13} />重新检查</button></div></div>
  }
  if (!candidate) {
    return <div className="pending-run candidate-output-empty"><FileText size={15} /><div><strong>选择一个 Run 后查看候选</strong><span>候选输出会在完成并持久化后显示。</span></div></div>
  }
  const authorReviewChangeSet = writerTrace.result?.mode === 'author_review_change_proposal'
  const providerEvidence = providerTurnReceipts.evidence
  const hasProviderIdentity = candidate.schema !== 'zhimeng.agent-candidate-output.v1'
  const providerIdentityLabel = hasProviderIdentity
    ? providerTurnReceipts.state === 'ready' && providerEvidence
      ? `${providerEvidence.provider_identity_status} · ${providerEvidence.provider_turn_receipts.length} turn${providerEvidence.provider_turn_receipts.length === 1 ? '' : 's'}`
      : providerTurnReceipts.state === 'loading'
        ? 'validating · 完整收据校验中'
        : providerTurnReceipts.state === 'missing' || providerTurnReceipts.state === 'error'
          ? 'invalid_evidence · 未通过交叉校验'
          : 'binding_only · 尚未交叉校验'
    : 'legacy_unverified · v1 历史记录'
  return (
    <div className="candidate-output-panel">
      <article className="candidate-output-card">
        <header className="candidate-output-header">
          <FileText size={17} />
          <div><span>Agent 候选 · {candidateKindLabel(candidate.kind)} · kind={candidate.kind}</span><h3>{candidate.content.title}</h3><p>{candidate.content.summary}</p></div>
          <strong className="candidate-review-badge">待作者审查</strong>
        </header>
        <div className="candidate-policy" role="status">
          <ShieldAlert size={16} />
          <div><strong>{authorReviewChangeSet ? '正文候选尚未写回' : '正式正文未授权'}</strong><span>{authorReviewChangeSet ? `WriterTask 已授权受审 ChangeSet · formal_draft_allowed=false · writes_project_truth=false · requires_author_review=true` : `formal_draft_allowed=false · requires_author_review=true · allowed_output=${candidate.allowed_output} · 仅允许修复方案或风险草稿`}</span></div>
        </div>
        <section className="candidate-body" aria-label="候选内容"><SafeMarkdown content={candidate.content.body} /></section>
        {candidate.failure_signatures.length > 0 && <section className="candidate-failures"><strong>门禁与失败签名</strong><ul>{candidate.failure_signatures.map((signature) => <li key={signature}><code>{signature}</code></li>)}</ul></section>}
        <details className="candidate-disclosure" open>
          <summary>来源绑定</summary>
          <div className="candidate-provenance-grid">
            <div><span>Run</span><code>{candidate.run_id}</code></div>
            <div><span>Workflow Plan</span><code>{candidate.workflow_plan_id}</code></div>
            <div><span>Final Checkpoint</span><code>{candidate.final_checkpoint_id}</code></div>
            <div><span>Provider boundary</span><code>{candidate.provider_boundary_id}</code></div>
            <div><span>Provider 身份</span><code>{providerIdentityLabel}</code></div>
            {candidate.schema === 'zhimeng.agent-candidate-output.v3' && <><div><span>Writer Task</span><code>{candidate.writer_task_id}</code></div><div><span>指令物化</span><code>{candidate.instruction_materialization_id}</code></div><div><span>调度收据</span><code>{candidate.instruction_dispatch_receipt_id}</code></div></>}
            <div><span>Context Pack</span><code>{candidate.context_pack_id}</code></div>
            <div><span>Operation Package</span><code>{candidate.operation_package_id}</code></div>
            <div><span>项目快照</span><code>{candidate.project_id} · revision {candidate.project_revision}</code></div>
            <div><span>Handoff 状态</span><code>{candidate.handoff_status}</code></div>
            <div><span>创建时间</span><code>{candidate.created_at}</code></div>
          </div>
        </details>
        {hasProviderIdentity && providerTurnReceipts.state === 'loading' && <div className="pending-run"><FileText size={13} /><div><strong>正在校验 Provider 身份收据</strong><span>逐项比对 Candidate binding、Boundary、模型身份和回合链 Hash</span></div></div>}
        {hasProviderIdentity && ['missing', 'error'].includes(providerTurnReceipts.state) && <div className="writer-trace-partial" role="alert"><AlertTriangle size={13} /><span>{providerTurnReceipts.error ?? 'Provider 身份收据未通过交叉校验'}</span><button type="button" className="quiet-button" onClick={providerTurnReceipts.refresh}><RefreshCw size={13} />重试</button></div>}
        {hasProviderIdentity && providerTurnReceipts.state === 'ready' && providerEvidence && <details className="candidate-disclosure" open>
          <summary>Provider 身份收据 · {providerEvidence.provider_turn_receipts.length}</summary>
          <div className="candidate-provenance-grid"><div><span>请求模型</span><code>{providerEvidence.requested_model ?? '未指定'}</code></div><div><span>聚合状态</span><code>{providerEvidence.provider_identity_status}</code></div></div>
          <ul className="candidate-reference-list">{providerEvidence.provider_turn_receipts.map((receipt) => <li key={receipt.receipt_id}><span>turn {receipt.turn_ordinal} · {receipt.model_identity_status}</span><code>{receipt.receipt_id}</code><small title={receipt.receipt_hash}>{receipt.requested_model ?? '未指定'} → {receipt.reported_model ?? '未报告'} · {receipt.finish_reason} · {receipt.usage ? `${receipt.usage.total_tokens} tokens · ` : ''}{compactHash(receipt.receipt_hash)}</small></li>)}</ul>
        </details>}
        {hasProviderIdentity && providerTurnReceipts.state === 'idle' && <div className="writer-trace-partial" role="status"><AlertTriangle size={13} /><span>Candidate 仅含 Provider Receipt binding，完整身份收据尚未加载。</span></div>}
        {candidate.schema === 'zhimeng.agent-candidate-output.v1' && <div className="writer-trace-partial" role="status"><AlertTriangle size={13} /><span>legacy_unverified：历史 v1 候选没有 Provider 响应身份收据，不补造验证结论。</span></div>}
        <details className="candidate-disclosure">
          <summary><Link2 size={13} />引用 · {candidate.references.length}</summary>
          <ul className="candidate-reference-list">{candidate.references.map((reference) => <li key={`${reference.reference_kind}:${reference.reference_id}`}><span>{candidateReferenceLabel(reference.reference_kind)}</span><code>{reference.reference_id}</code><small title={reference.reference_hash}>{compactHash(reference.reference_hash)}</small></li>)}</ul>
        </details>
        <details className="candidate-disclosure">
          <summary>完整性 Hash</summary>
          <div className="candidate-hash-list"><div><span>workflow_plan_hash</span><code>{candidate.workflow_plan_hash}</code></div><div><span>final_checkpoint_hash</span><code>{candidate.final_checkpoint_hash}</code></div><div><span>provider_boundary_hash</span><code>{candidate.provider_boundary_hash}</code></div><div><span>provider_request_hash</span><code>{candidate.provider_request_hash}</code></div>{hasProviderIdentity && <div><span>provider_turn_receipts_hash</span><code>{candidate.provider_turn_receipts_hash}</code></div>}{candidate.schema === 'zhimeng.agent-candidate-output.v3' && <><div><span>writer_task_hash</span><code>{candidate.writer_task_hash}</code></div><div><span>instruction_materialization_hash</span><code>{candidate.instruction_materialization_hash}</code></div><div><span>instruction_dispatch_receipt_hash</span><code>{candidate.instruction_dispatch_receipt_hash}</code></div></>}<div><span>context_pack_hash</span><code>{candidate.context_pack_hash}</code></div><div><span>operation_package_hash</span><code>{candidate.operation_package_hash}</code></div><div><span>project_fingerprint</span><code>{candidate.project_fingerprint}</code></div><div><span>content_hash</span><code>{candidate.content_hash}</code></div><div><span>references_hash</span><code>{candidate.references_hash}</code></div><div><span>envelope_hash</span><code>{candidate.envelope_hash}</code></div></div>
        </details>
      </article>
    </div>
  )
}

export function RunDrawer({
  demo,
  conversation,
  onReviewChangeSet,
  candidateOutput = emptyCandidateOutput,
  providerTurnReceipts = emptyProviderTurnReceipts,
  writerTrace = emptyWriterTrace,
  providerRecovery,
  candidateReviewRequestKey = 0,
}: RunDrawerProps) {
  const [open, setOpen] = useState(false)
  const [tab, setTab] = useState<DrawerTab>('timeline')
  const toolPairs = useMemo(
    () => pairToolActivities(conversation.activities),
    [conversation.activities],
  )
  const artifacts = conversation.activities.filter((activity) => activity.kind === 'artifact')
  const skills = conversation.activities.filter((activity) => activity.kind === 'skill_call')
  const commands = conversation.activities.filter((activity) => activity.kind === 'command')

  useEffect(() => {
    if (candidateReviewRequestKey <= 0) return
    setTab('candidate')
    setOpen(true)
  }, [candidateReviewRequestKey])

  const chooseTab = (nextTab: DrawerTab) => {
    setTab(nextTab)
    setOpen(true)
  }

  const reviewChangeSet = (changeSetId: string) => {
    setOpen(false)
    onReviewChangeSet?.(changeSetId)
  }

  const tabCount = (tabId: DrawerTab): number => {
    if (demo) return tabId === 'timeline' ? 4 : 0
    if (tabId === 'timeline') return conversation.activities.length
    if (tabId === 'tools') return toolPairs.length
    if (tabId === 'skills') return skills.length
    if (tabId === 'candidate') {
      return Number(candidateOutput.output !== null) + Number(writerTrace.task !== null) + Number(providerRecovery?.response !== null && providerRecovery?.response !== undefined)
    }
    if (tabId === 'artifacts') return artifacts.length
    return commands.length
  }

  const activeRunStatus = conversation.activeRun?.status
  return (
    <section className={`run-drawer${open ? ' is-open' : ''}`} aria-label="运行抽屉">
      <header className="drawer-header">
        <div className="drawer-tabs" role="tablist" aria-label="运行详情">
          {tabs.map((item) => {
            const count = tabCount(item.id)
            return <button type="button" role="tab" aria-selected={tab === item.id && open} className={tab === item.id && open ? 'is-active' : ''} key={item.id} onClick={() => chooseTab(item.id)}>{item.icon}{item.label}{count > 0 && <span className="count-mark">{count}</span>}</button>
          })}
        </div>
        <div className="drawer-run"><span className={`run-dot run-${activeRunStatus ?? 'idle'}`} />{demo ? '示例运行' : conversation.activeRun ? `Run ${conversation.activeRun.ordinal}` : '无活动'}<span>·</span><span>{demo ? '未启动' : activeRunStatus ? runStatusLabels[activeRunStatus] : '空闲'}</span><button className="icon-button" type="button" aria-label={open ? '收起运行抽屉' : '展开运行抽屉'} title={open ? '收起' : '展开'} onClick={() => setOpen((value) => !value)}>{open ? <ChevronDown size={16} /> : <ChevronUp size={16} />}</button></div>
      </header>
      {open && !demo && <div className="drawer-content" role="tabpanel" aria-label={tabs.find((item) => item.id === tab)?.label}>
        {conversation.activeRun?.error && <div className="run-error-banner" role="alert"><AlertTriangle size={14} /><div><strong>{conversation.activeRun.error.error.code}</strong><span>{conversation.activeRun.error.error.message}</span></div></div>}
        {tab === 'candidate' && <>
          {providerRecovery && providerRecovery.state !== 'idle' && <ProviderRecoveryPanel snapshot={providerRecovery} onDecisionCommitted={conversation.refresh} onRecompile={conversation.activeRun ? () => void conversation.compileContext(conversation.activeRun!.objective).catch(() => undefined) : undefined} recompileDisabled={conversation.busy || conversation.pendingContextPack !== null} />}
          <WriterTracePanel snapshot={writerTrace} />
          <CandidateOutputPanel snapshot={candidateOutput} providerTurnReceipts={providerTurnReceipts} writerTrace={writerTrace} />
        </>}
        {tab === 'timeline' && (conversation.activities.length > 0 ? <div className="timeline-table" role="list">{conversation.activities.map((activity) => <article className={`timeline-row timeline-${activity.kind}`} role="listitem" key={activity.activity_id}><time>{new Date(activity.created_at).toLocaleTimeString('zh-CN', { hour12: false })}</time><span className={`event-icon event-${activity.kind}`}>{activityIcon(activity)}</span><strong>{activityLabels[activity.kind]}</strong><span className="timeline-detail">{activityDetail(activity)}</span>{activity.kind === 'change_proposal' && onReviewChangeSet && <button type="button" className="quiet-button timeline-review" onClick={() => reviewChangeSet(activity.payload.proposal_id)}><FileDiff size={13} />审阅改动</button>}</article>)}</div> : <div className="pending-run"><Box size={15} /><div><strong>当前没有 Agent 活动</strong><span>尚未开始运行</span></div></div>)}
        {tab === 'tools' && (toolPairs.length > 0 ? <div className="tool-activity-list">{toolPairs.map((pair) => <ToolActivityCard pair={pair} key={pair.toolCallId} />)}</div> : <div className="pending-run"><Hammer size={15} /><div><strong>当前 Run 没有工具调用</strong><span>0 条记录</span></div></div>)}
        {tab === 'skills' && (skills.length > 0 ? <div className="skill-activity-list">{skills.map((activity) => <SkillActivityCard activity={activity} key={activity.payload.skill_call_id} />)}</div> : <div className="pending-run"><BadgeCheck size={15} /><div><strong>当前 Run 没有确定性 Skill 调用</strong><span>0 条记录</span></div></div>)}
        {tab === 'artifacts' && (artifacts.length > 0 ? <div className="artifact-grid">{artifacts.map((activity) => <article className="artifact-item" key={activity.activity_id}><FileArchive size={16} /><span><strong>{activity.payload.name}</strong><small>{activity.payload.artifact_type} · {formatBytes(activity.payload.size_bytes)} · {compactHash(activity.payload.content_hash)}</small></span></article>)}</div> : <div className="pending-run"><FileArchive size={15} /><div><strong>当前 Run 没有运行产物</strong><span>0 条记录</span></div></div>)}
        {tab === 'terminal' && (commands.length > 0 ? <div className="terminal-panel">{commands.map((activity) => <div key={activity.activity_id}><span className="terminal-prompt">{activity.payload.status}</span><span>{activity.payload.command}</span></div>)}</div> : <div className="pending-run"><TerminalSquare size={15} /><div><strong>当前 Run 没有命令活动</strong><span>0 条记录</span></div></div>)}
      </div>}
      {open && demo && <div className="drawer-content" role="tabpanel" aria-label={tabs.find((item) => item.id === tab)?.label}>
        {tab === 'candidate' && <div className="pending-run candidate-output-empty"><FileText size={15} /><div><strong>示例模式没有 Agent 候选输出</strong><span>真实 Run 完成后才会读取持久化候选。</span></div></div>}
        {tab === 'timeline' && <div className="timeline-table">{timelineEvents.map((event) => <div className="timeline-row" key={`${event.time}-${event.type}`}><time>{event.time}</time><span className={`event-icon event-${event.type}`}><Box size={12} /></span><strong>{event.label}</strong><span>{event.detail}</span></div>)}</div>}
        {tab === 'tools' && <div className="drawer-table"><div className="drawer-table-head"><span>工具</span><span>权限</span><span>状态</span><span>耗时</span></div><div><span>project.read</span><span>只读 · 当前项目</span><strong className="text-ok">完成</strong><span>42 ms</span></div><div><span>context.assemble</span><span>staging</span><strong className="text-warn">等待 Runtime</strong><span>—</span></div></div>}
        {tab === 'skills' && <div className="pending-run"><BadgeCheck size={15} /><div><strong>示例模式没有确定性 Skill 收据</strong><span>0 条记录</span></div></div>}
        {tab === 'artifacts' && <div className="artifact-grid"><button type="button"><FileArchive size={16} /><span><strong>context_pack.json</strong><small>8.1k tokens · sha256 6e4a…</small></span></button><button type="button"><FileArchive size={16} /><span><strong>change_plan.json</strong><small>暂存 · 尚未生成 Diff</small></span></button></div>}
        {tab === 'terminal' && <div className="terminal-panel"><div><span className="terminal-prompt">runtime</span><span>连接后显示受限命令输出</span></div><div><span className="terminal-prompt">policy</span><span>开发者模式关闭 · shell 调用需要单次审批</span></div></div>}
      </div>}
    </section>
  )
}

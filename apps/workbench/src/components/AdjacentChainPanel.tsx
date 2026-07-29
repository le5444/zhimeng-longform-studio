import {
  AlertTriangle,
  CheckCircle2,
  FileSearch,
  GitBranchPlus,
  Link2,
  LoaderCircle,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
} from 'lucide-react'
import { useAdjacentChain } from '../hooks/useAdjacentChain'
import type { AdjacentChainRecord } from '../api/adjacentChains'

interface AdjacentChainPanelProps {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  sourceImportId?: string | null
  activeChapterId?: string | null
  onOpenContextReview?: (contextPackId: string) => void | Promise<void>
  openContextReviewBusy?: boolean
}

const statusCopy: Record<AdjacentChainRecord['status'], { title: string; detail: string }> = {
  awaiting_upstream: {
    title: '等待正式知识批次',
    detail: '先完成 work / voice / validator 正式化，再连续执行 O2-D → O2-E。',
  },
  prepared: {
    title: '相邻链已准备',
    detail: '一次批准同时覆盖 distillation 交接与 Context Pack 装配，不写正文。',
  },
  approved: {
    title: '已批准，等待连续运行',
    detail: '作者已确认风险；运行后会先做 O2-D，再自动做 O2-E。',
  },
  rejected: {
    title: '已拒绝',
    detail: '相邻链不会执行，可重新准备后再审批。',
  },
  disabled: {
    title: '已禁用',
    detail: '作者禁用了相邻链，Runtime 会 fail closed。',
  },
  running: {
    title: '相邻链执行中',
    detail: 'Runtime 正按固定顺序执行 O2-D 与 O2-E，并保留逐步状态。',
  },
  succeeded: {
    title: '连续交接完成',
    detail: '已生成受控 distillation_index 入口，并装配写前 Context Pack。',
  },
  failed: {
    title: '相邻链失败',
    detail: '中途失败不会假装成功；按错误修复后重新准备。',
  },
}

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
}

function stepStatusLabel(status: AdjacentChainRecord['steps'][number]['status']): string {
  if (status === 'succeeded') return '完成'
  if (status === 'running') return '运行中'
  if (status === 'approved') return '已批准'
  if (status === 'prepared') return '已准备'
  if (status === 'failed') return '失败'
  if (status === 'skipped') return '跳过'
  return '等待'
}

export function AdjacentChainPanel({
  enabled,
  projectId,
  projectRevision = null,
  projectFingerprint = null,
  sourceImportId = null,
  activeChapterId = null,
  onOpenContextReview,
  openContextReviewBusy = false,
}: AdjacentChainPanelProps) {
  const chain = useAdjacentChain(enabled, projectId, projectRevision, projectFingerprint, activeChapterId)
  const record = chain.record
  const status = record?.status ?? 'awaiting_upstream'
  const copy = statusCopy[status]
  const canPrepare = Boolean(enabled && projectId && projectRevision && projectFingerprint)
  const canApprove = record?.status === 'prepared' && !chain.busy
  const canRun = record?.status === 'approved' && !chain.busy
  const canReject = record && ['prepared', 'approved', 'failed', 'rejected'].includes(record.status) && !chain.busy
  const canDisable = record && record.status !== 'running' && record.status !== 'disabled' && !chain.busy
  const canOpenContextReview = Boolean(
    record?.status === 'succeeded'
    && record.context_pack_id
    && onOpenContextReview
    && !chain.busy
    && !openContextReviewBusy,
  )
  const steps = record?.steps ?? []

  return (
    <section className="adjacent-chain-panel skill-handoff-panel" aria-label="相邻 Skill 连续链">
      <header className={`skill-handoff-hero status-${status}`}>
        <span className="skill-handoff-icon">
          {chain.busy || status === 'running' || chain.state === 'loading'
            ? <LoaderCircle size={16} className="spin" />
            : status === 'succeeded'
              ? <CheckCircle2 size={16} />
              : status === 'failed' || status === 'disabled' || status === 'rejected'
                ? <AlertTriangle size={16} />
                : <GitBranchPlus size={16} />}
        </span>
        <div>
          <span className="panel-kicker">O2-D → O2-E 相邻连续链</span>
          <h3>{copy.title}</h3>
          <p>{copy.detail}</p>
        </div>
        <button type="button" className="icon-button" aria-label="刷新相邻链" onClick={() => void chain.refresh()} disabled={!enabled || chain.busy}>
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="skill-handoff-flow" aria-label="相邻 Skill 连续路径">
        <div><strong>O2-D</strong><span>distillation → KB 入口</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>单次风险批准</strong><span>作者确认，不写正文</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>O2-E</strong><span>KB 入口 → Context Pack</span></div>
      </div>

      {chain.error && (
        <div className="skill-handoff-error" role="alert">
          <AlertTriangle size={15} />
          <span>{chain.error}</span>
        </div>
      )}

      {steps.length > 0 ? (
        <div className="adjacent-chain-steps" aria-label="相邻链步骤">
          {steps.map((step) => (
            <article key={step.step_id} data-status={step.status}>
              <span>步骤 {step.ordinal}</span>
              <strong>{step.title}</strong>
              <p>{step.skill_id} · {step.capability}</p>
              <small>{stepStatusLabel(step.status)}{step.child_id ? ` · ${step.child_id.slice(0, 18)}…` : ''}</small>
            </article>
          ))}
        </div>
      ) : (
        <p className="skill-handoff-empty">还没有相邻链步骤。先准备连续交接。</p>
      )}

      <div className="skill-handoff-permissions" aria-label="权限与风险">
        <div><LockKeyhole size={14} /><strong>权限</strong><span>只读项目快照与 staging；可写 staging 与 Context store；无网络。</span></div>
        <div><ShieldCheck size={14} /><strong>边界</strong><span>固定两步相邻链，不是通用四 Skills exactly-once；formal_draft_allowed 保持 false。</span></div>
        <div><Link2 size={14} /><strong>复用</strong><span>底层仍走既有 O2-D / O2-E handoff，可单独恢复 receipt。</span></div>
      </div>

      {record?.risk_dossier && (
        <div className="adjacent-risk-dossier" aria-label="风险审批 dossier">
          <strong>风险审批绑定</strong>
          <p>{record.risk_dossier.risk_summary}</p>
          <ul>
            {record.risk_dossier.skills.map((skill) => (
              <li key={skill.step_id}>
                <code>{skill.skill_id}</code>
                <span>@{skill.skill_version}</span>
                <small>{skill.input_contract.split('/').at(-1)} → {skill.output_contract.split('/').at(-1)}</small>
              </li>
            ))}
          </ul>
          {record.risk_approval_token && (
            <div className="adjacent-risk-token" data-consumed={record.risk_approval_token.consumed ? 'true' : 'false'}>
              <span>token {record.risk_approval_token.consumed ? '已消费' : '待运行消费'}</span>
              <code>{record.risk_approval_token.token_id}</code>
            </div>
          )}
        </div>
      )}

      {record?.workflow_plan && (
        <div className="adjacent-workflow-plan" aria-label="相邻 Workflow Plan">
          <strong>Workflow Plan</strong>
          <p>
            与 Creation durable workflow 共用 managed_skill step 原语；固定两步，不宣称通用 exactly-once。
          </p>
          <code>{record.workflow_plan.workflow_plan_id}</code>
          <ul>
            {record.workflow_plan.steps.map((step) => (
              <li key={step.workflow_step_id}>
                <span>步骤 {step.ordinal}</span>
                <code>{step.skill_id}</code>
                <small>{step.workflow_step_id}</small>
              </li>
            ))}
          </ul>
          {record.workflow_checkpoints.length > 0 && (
            <div className="adjacent-workflow-checkpoints" aria-label="Workflow checkpoints">
              {record.workflow_checkpoints.map((checkpoint) => (
                <article key={checkpoint.checkpoint_id} data-status={checkpoint.execution_status}>
                  <strong>checkpoint #{checkpoint.step_ordinal}</strong>
                  <span>{checkpoint.execution_status}</span>
                  <code>{checkpoint.checkpoint_id}</code>
                </article>
              ))}
            </div>
          )}
        </div>
      )}

      {record && status === 'succeeded' && (
        <div className="skill-handoff-result" role="status" aria-label="相邻链已完成">
          <CheckCircle2 size={16} />
          <div>
            <strong>Context Pack 已装配</strong>
            <span>{record.context_pack_id ?? 'context-pack'} · route {record.route_receipt_id ?? 'none'}</span>
            <code>{record.context_pack_hash}</code>
            <p className="adjacent-chain-next">
              下一步由作者审查写前 Context Pack；formal_draft_allowed 仍为 false，不写正式正文。
            </p>
            {onOpenContextReview && (
              <button
                type="button"
                className="quiet-button adjacent-chain-open-context"
                disabled={!canOpenContextReview}
                onClick={() => {
                  if (!record.context_pack_id) return
                  void onOpenContextReview(record.context_pack_id)
                }}
              >
                <FileSearch size={14} />打开 Context 审查
              </button>
            )}
          </div>
        </div>
      )}

      <div className="skill-handoff-actions">
        <button type="button" className="quiet-button" disabled={!canPrepare || chain.busy} onClick={() => void chain.prepare(sourceImportId)}>
          <SlidersHorizontal size={14} />准备连续链
        </button>
        <button type="button" className="quiet-button" disabled={!canApprove} onClick={() => void chain.approve()}>
          <ShieldCheck size={14} />批准一次
        </button>
        <button type="button" className="quiet-button" disabled={!canReject} onClick={() => void chain.reject()}>
          <XCircle size={14} />拒绝
        </button>
        <button type="button" className="quiet-button" disabled={!canDisable} onClick={() => void chain.disable()}>
          <LockKeyhole size={14} />禁用
        </button>
        <button type="button" className="full-action" disabled={!canRun} onClick={() => void chain.run()}>
          <Play size={14} fill="currentColor" />连续运行 O2-D→O2-E
        </button>
      </div>

      {record && (
        <details className="skill-handoff-tech">
          <summary>技术详情 / chain / child handoff</summary>
          <dl>
            <div><dt>chain_id</dt><dd><code>{record.chain_id}</code></dd></div>
            <div><dt>skill_handoff_id</dt><dd><code>{record.skill_handoff_id ?? 'none'}</code></dd></div>
            <div><dt>context_handoff_id</dt><dd><code>{record.context_handoff_id ?? 'none'}</code></dd></div>
            <div><dt>context_pack</dt><dd><code>{record.context_pack_id ?? 'none'}</code></dd></div>
            <div><dt>context_pack hash</dt><dd><code>{shortHash(record.context_pack_hash)}</code></dd></div>
            <div><dt>route receipt</dt><dd><code>{record.route_receipt_id ?? 'none'}</code></dd></div>
            <div><dt>source_import</dt><dd><code>{record.source_import_id ?? 'none'}</code></dd></div>
            <div><dt>current step</dt><dd><code>{record.current_step_ordinal}</code></dd></div>
          </dl>
        </details>
      )}
    </section>
  )
}

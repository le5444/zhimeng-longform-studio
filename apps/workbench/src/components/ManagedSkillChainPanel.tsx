import {
  AlertTriangle,
  CheckCircle2,
  GitBranchPlus,
  LoaderCircle,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  XCircle,
} from 'lucide-react'
import { useManagedSkillChain } from '../hooks/useManagedSkillChain'
import type { ManagedSkillChainRunRecord } from '../api/managedSkillChains'

interface ManagedSkillChainPanelProps {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
}

const statusCopy: Record<ManagedSkillChainRunRecord['status'], { title: string; detail: string }> = {
  prepared: {
    title: '通用链已准备',
    detail: '一次风险批准可覆盖本链全部 managed deterministic 步骤；不写正式正文。',
  },
  approved: {
    title: '已批准，等待运行',
    detail: '作者已确认风险 dossier；运行后按顺序执行并写 checkpoint 链。',
  },
  rejected: {
    title: '已拒绝',
    detail: '链不会执行；可重新准备。',
  },
  disabled: {
    title: '已禁用',
    detail: '作者禁用了该链，Runtime fail closed。',
  },
  running: {
    title: '执行中',
    detail: 'Runtime 正按 durable plan 逐步执行。',
  },
  succeeded: {
    title: '执行完成',
    detail: '逐步收据与 checkpoint 已落盘；formal_draft_allowed 仍为 false。',
  },
  failed: {
    title: '执行失败',
    detail: '中途失败不会假装成功；修复后重新准备。',
  },
}

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
}

function stepStatusLabel(status: ManagedSkillChainRunRecord['steps'][number]['status']): string {
  if (status === 'succeeded') return '完成'
  if (status === 'running') return '运行中'
  if (status === 'approved') return '已批准'
  if (status === 'prepared') return '已准备'
  if (status === 'failed') return '失败'
  if (status === 'skipped') return '跳过'
  return '等待'
}

export function ManagedSkillChainPanel({
  enabled,
  projectId,
  projectRevision = null,
  projectFingerprint = null,
}: ManagedSkillChainPanelProps) {
  const chain = useManagedSkillChain(enabled, projectId, projectRevision, projectFingerprint)
  const record = chain.record
  const status = record?.status
  const copy = status ? statusCopy[status] : {
    title: '通用 Managed Skill Chain',
    detail: '声明 N 步受管 deterministic skill，一次风险批准后连续执行。不是原始四 Skills exactly-once。',
  }
  const canPrepare = Boolean(enabled && projectId && projectRevision && projectFingerprint && !chain.busy)
  const canApprove = record?.status === 'prepared' && !chain.busy
  const canRun = record?.status === 'approved' && !chain.busy
  const canReject = Boolean(record && ['prepared', 'approved', 'failed', 'rejected'].includes(record.status) && !chain.busy)
  const canDisable = Boolean(record && record.status !== 'running' && record.status !== 'disabled' && !chain.busy)

  return (
    <section className="managed-skill-panel skill-handoff-panel" aria-label="通用 Managed Skill Chain">
      <header className={`skill-handoff-hero status-${chain.state === 'error' || status === 'failed' ? 'failed' : status === 'succeeded' ? 'succeeded' : 'prepared'}`}>
        <span className="skill-handoff-icon">
          {chain.busy || chain.state === 'loading'
            ? <LoaderCircle size={16} className="spin" />
            : status === 'succeeded'
              ? <CheckCircle2 size={16} />
              : status === 'failed'
                ? <AlertTriangle size={16} />
                : <GitBranchPlus size={16} />}
        </span>
        <div>
          <span className="panel-kicker">M3w 通用 Managed Chain</span>
          <h3>{copy.title}</h3>
          <p>{copy.detail}</p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新通用 skill chain"
          disabled={!record || chain.busy}
          onClick={() => void chain.refresh()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="skill-handoff-flow" aria-label="通用链路径">
        <div><strong>准备步骤</strong><span>1..8 managed deterministic</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>风险批准</strong><span>一次 token 覆盖本链</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>逐步执行</strong><span>checkpoint 哈希链</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>边界</strong><span>formal_draft_allowed=false</span></div>
      </div>

      <div className="skill-handoff-actions">
        <button
          type="button"
          className="quiet-button"
          disabled={!canPrepare}
          onClick={() => void chain.prepareDualProjectAudit().catch(() => undefined)}
        >
          {chain.busy && !record ? <LoaderCircle size={13} className="spin" /> : <GitBranchPlus size={13} />}
          准备双步项目审计
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={!canApprove}
          onClick={() => void chain.approve().catch(() => undefined)}
        >
          <ShieldCheck size={13} />批准风险
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={!canRun}
          onClick={() => void chain.run().catch(() => undefined)}
        >
          {chain.busy && record?.status === 'approved' ? <LoaderCircle size={13} className="spin" /> : <Play size={13} />}
          运行链
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={!canReject}
          onClick={() => void chain.reject().catch(() => undefined)}
        >
          <XCircle size={13} />拒绝
        </button>
        <button
          type="button"
          className="quiet-button"
          disabled={!canDisable}
          onClick={() => void chain.disable().catch(() => undefined)}
        >
          <LockKeyhole size={13} />禁用
        </button>
      </div>

      {chain.error && (
        <div className="agent-inline-error" role="alert">
          <AlertTriangle size={14} />
          <span>{chain.error}</span>
        </div>
      )}

      {!enabled && (
        <div className="distillation-empty"><AlertTriangle size={16} /><span>Writing Runtime 未就绪</span></div>
      )}
      {enabled && !projectId && (
        <div className="distillation-empty"><AlertTriangle size={16} /><span>请先绑定小说项目</span></div>
      )}

      {record && (
        <div className="skill-handoff-details" aria-label="通用链详情">
          <dl>
            <div><dt>chain_id</dt><dd><code>{record.chain_id}</code></dd></div>
            <div><dt>chain_key</dt><dd><code>{record.chain_key}</code></dd></div>
            <div><dt>status</dt><dd><code>{record.status}</code></dd></div>
            <div><dt>formal_draft_allowed</dt><dd><code>{String(record.formal_draft_allowed)}</code></dd></div>
            <div><dt>current_step</dt><dd><code>{record.current_step_ordinal}/{record.steps.length}</code></dd></div>
            <div><dt>risk_token</dt><dd><code>{shortHash(record.risk_approval_token?.token_hash)}</code></dd></div>
            <div><dt>plan_hash</dt><dd><code>{shortHash(record.workflow_plan?.plan_hash)}</code></dd></div>
          </dl>

          <section aria-label="步骤状态">
            <h4>步骤</h4>
            <ul className="managed-skill-chain-steps">
              {record.steps.map((step) => (
                <li key={step.step_id}>
                  <strong>{step.ordinal}. {step.title}</strong>
                  <span>{step.skill_id}</span>
                  <span>{stepStatusLabel(step.status)}</span>
                  {step.skill_receipt_id && <code>{step.skill_receipt_id.slice(0, 22)}</code>}
                  {step.error_message && <em>{step.error_message}</em>}
                </li>
              ))}
            </ul>
          </section>

          {record.risk_dossier && (
            <section aria-label="风险说明">
              <h4>风险 dossier</h4>
              <p>{record.risk_dossier.risk_summary}</p>
              <ul>
                {record.risk_dossier.not_claiming.map((item) => (
                  <li key={item}><code>{item}</code></li>
                ))}
              </ul>
            </section>
          )}

          {chain.notClaiming.length > 0 && (
            <section aria-label="不宣称范围">
              <h4>响应 not_claiming</h4>
              <ul>
                {chain.notClaiming.map((item) => (
                  <li key={item}><code>{item}</code></li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </section>
  )
}

import {
  AlertTriangle,
  CheckCircle2,
  GitBranch,
  LoaderCircle,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
} from 'lucide-react'
import { useSkillHandoff } from '../hooks/useSkillHandoff'
import type { SkillHandoffCardRef, SkillHandoffRecord } from '../api/skillHandoffs'

interface SkillHandoffPanelProps {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  sourceImportId?: string | null
}

const statusCopy: Record<SkillHandoffRecord['status'], { title: string; detail: string }> = {
  awaiting_upstream: {
    title: '等待正式知识批次',
    detail: '先完成 novel-distillation 的三类知识正式化，再把入口交给 novel-kb-manager。',
  },
  prepared: {
    title: '待作者批准',
    detail: 'Runtime 已把上游产物、版本与下游 Skill 权限绑定，批准后才会运行。',
  },
  approved: {
    title: '已批准，等待运行',
    detail: '作者已确认风险；下一步只生成 distillation_index 交接产物，不写正文。',
  },
  rejected: {
    title: '已拒绝',
    detail: '本次相邻交接不会路由，可重新准备后再审批。',
  },
  disabled: {
    title: '已禁用',
    detail: '作者禁用了该交接，Runtime 会 fail closed，不会自动路由。',
  },
  running: {
    title: '正在运行受管 adapter',
    detail: 'Runtime 正在执行包内 snapshot，不依赖用户级外部 Skill 路径。',
  },
  succeeded: {
    title: '交接产物已生成',
    detail: '上游正式知识已成为 novel-kb-manager 的受控输入入口。',
  },
  failed: {
    title: '交接失败',
    detail: '没有生成可消费入口；需要按错误修复后重新准备或运行。',
  },
}

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
}

function cardTypeLabel(type: SkillHandoffCardRef['card_type']): string {
  if (type === 'work') return '作品机制'
  if (type === 'voice') return '叙事声音'
  return '正文校验'
}

export function SkillHandoffPanel({ enabled, projectId, projectRevision = null, projectFingerprint = null, sourceImportId = null }: SkillHandoffPanelProps) {
  const handoff = useSkillHandoff(enabled, projectId, projectRevision, projectFingerprint)
  const record = handoff.record
  const status = record?.status ?? 'awaiting_upstream'
  const copy = statusCopy[status]
  const canPrepare = Boolean(enabled && projectId && projectRevision && projectFingerprint)
  const canApprove = record?.status === 'prepared' && !handoff.busy
  const canRun = record?.status === 'approved' && !handoff.busy
  const canReject = record && ['prepared', 'approved', 'failed', 'rejected'].includes(record.status) && !handoff.busy
  const canDisable = record && record.status !== 'running' && record.status !== 'disabled' && !handoff.busy
  const cards = record?.upstream?.cards ?? []

  return (
    <section className="skill-handoff-panel" aria-label="原始 Skill 相邻交接">
      <header className={`skill-handoff-hero status-${status}`}>
        <span className="skill-handoff-icon">
          {handoff.busy || status === 'running' || handoff.state === 'loading'
            ? <LoaderCircle size={16} className="spin" />
            : status === 'succeeded'
              ? <CheckCircle2 size={16} />
              : status === 'failed' || status === 'disabled' || status === 'rejected'
                ? <AlertTriangle size={16} />
                : <GitBranch size={16} />}
        </span>
        <div>
          <span className="panel-kicker">O2-D 原始 Skill 相邻 handoff</span>
          <h3>{copy.title}</h3>
          <p>{copy.detail}</p>
        </div>
        <button type="button" className="icon-button" aria-label="刷新 Skill 交接" onClick={() => void handoff.refresh()} disabled={!enabled || handoff.busy}>
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="skill-handoff-flow" aria-label="上游产物到下游 Skill">
        <div><strong>novel-distillation</strong><span>正式 work / voice / validator</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>Runtime 受管 adapter</strong><span>审批、版本、receipt、checkpoint</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>novel-kb-manager</strong><span>import-distillation-index</span></div>
      </div>

      {handoff.error && <div className="skill-handoff-error" role="alert"><AlertTriangle size={15} /><span>{handoff.error}</span></div>}

      {cards.length > 0 ? (
        <div className="skill-handoff-cards" aria-label="上游正式知识卡">
          {cards.map((card) => (
            <article key={card.card_id}>
              <span>{cardTypeLabel(card.card_type)}</span>
              <strong>{card.title}</strong>
              <p>{card.summary}</p>
              <small>{card.route_terms.length ? card.route_terms.join(' · ') : '未声明路由词'}</small>
            </article>
          ))}
        </div>
      ) : (
        <p className="skill-handoff-empty">当前项目没有可交接的正式三类知识；先在资料/知识页完成正式化。</p>
      )}

      <div className="skill-handoff-permissions" aria-label="权限与风险">
        <div><LockKeyhole size={14} /><strong>权限</strong><span>只读项目快照；只写 Runtime staging；无网络；无 secrets。</span></div>
        <div><ShieldCheck size={14} /><strong>边界</strong><span>只生成 distillation_index 入口，不复制卡全文，不写回正文。</span></div>
      </div>

      {record?.result && status === 'succeeded' && (
        <div className="skill-handoff-result" role="status" aria-label="Skill 交接已完成">
          <CheckCircle2 size={16} />
          <div>
            <strong>受控入口已生成</strong>
            <span>{record.result.downstream_target ?? 'novel-kb-manager.import-distillation-index'} · work/voice/validator = 1/1/1</span>
            <code>{record.result.distillation_index_hash}</code>
          </div>
        </div>
      )}

      <div className="skill-handoff-actions">
        <button type="button" className="quiet-button" disabled={!canPrepare || handoff.busy} onClick={() => void handoff.prepare(sourceImportId)}>
          <SlidersHorizontal size={14} />准备交接
        </button>
        <button type="button" className="quiet-button" disabled={!canApprove} onClick={() => void handoff.approve()}>
          <ShieldCheck size={14} />批准
        </button>
        <button type="button" className="quiet-button" disabled={!canReject} onClick={() => void handoff.reject()}>
          <XCircle size={14} />拒绝
        </button>
        <button type="button" className="quiet-button" disabled={!canDisable} onClick={() => void handoff.disable()}>
          <LockKeyhole size={14} />禁用
        </button>
        <button type="button" className="full-action" disabled={!canRun} onClick={() => void handoff.run()}>
          <Play size={14} fill="currentColor" />运行交接
        </button>
      </div>

      {record && (
        <details className="skill-handoff-tech">
          <summary>技术详情 / receipt / checkpoint</summary>
          <dl>
            <div><dt>handoff_id</dt><dd><code>{record.handoff_id}</code></dd></div>
            <div><dt>formalization</dt><dd><code>{record.upstream?.formalization_receipt_id ?? 'none'}</code></dd></div>
            <div><dt>receipt hash</dt><dd><code>{shortHash(record.upstream?.formalization_receipt_hash)}</code></dd></div>
            <div><dt>workflow plan</dt><dd><code>{record.workflow?.workflow_plan_id ?? 'none'}</code></dd></div>
            <div><dt>checkpoint</dt><dd><code>{record.workflow?.checkpoint_id ?? 'none'}</code></dd></div>
            <div><dt>skill receipt</dt><dd><code>{record.skill_receipt_id ?? 'none'}</code></dd></div>
            <div><dt>skill receipt hash</dt><dd><code>{shortHash(record.skill_receipt_hash)}</code></dd></div>
            <div><dt>adapter version</dt><dd><code>{record.activation?.skill_version ?? 'not active'}</code></dd></div>
          </dl>
        </details>
      )}
    </section>
  )
}

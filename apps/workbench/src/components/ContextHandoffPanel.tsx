import {
  AlertTriangle,
  BookOpenCheck,
  CheckCircle2,
  FileSearch,
  Layers3,
  LoaderCircle,
  LockKeyhole,
  Play,
  RefreshCw,
  ShieldCheck,
  SlidersHorizontal,
  XCircle,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { useContextHandoff } from '../hooks/useContextHandoff'
import type { ContextHandoffIndexEntry, ContextHandoffRecord } from '../api/contextHandoffs'

interface ContextHandoffPanelProps {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
}

const statusCopy: Record<ContextHandoffRecord['status'], { title: string; detail: string }> = {
  awaiting_upstream: {
    title: '等待受控入口',
    detail: '先完成 O2-D，把正式三类知识交成 distillation_index 入口。',
  },
  prepared: {
    title: 'Context Pack 待批准',
    detail: 'Runtime 已绑定 O2-D receipt、项目 revision 与 assemble-context 输入。',
  },
  approved: {
    title: '已批准，等待装配',
    detail: '作者已确认：只生成写前 Context Pack，不写正文，不写项目真值。',
  },
  rejected: {
    title: '已拒绝',
    detail: '本次 Context handoff 不会继续运行，可重新准备后再审批。',
  },
  disabled: {
    title: '已禁用',
    detail: '作者禁用了该下游路由；Runtime 会 fail closed。',
  },
  running: {
    title: '正在装配 Context Pack',
    detail: '受管 adapter 正在校验入口并生成 assemble-context intent。',
  },
  succeeded: {
    title: 'Context Pack 已生成',
    detail: '受控入口已经成为写前上下文包；route ready 不等于 validated。',
  },
  failed: {
    title: 'Context handoff 失败',
    detail: '没有生成可恢复 Context Pack；按错误修复后重新准备/运行。',
  },
}

function fullEvidence(value: string | null | undefined): string {
  return value || 'none'
}

function entryTypeLabel(type: ContextHandoffIndexEntry['card_type']): string {
  if (type === 'work') return '作品机制入口'
  if (type === 'voice') return '叙事声音入口'
  return '校验闸门入口'
}

function routeHealthCopy(value: string | undefined | null): string {
  if (value === 'ready') return '可装配：ready，但还不是 validated'
  if (value === 'risk_ready') return '可风险预备：需要作者继续补证'
  if (value === 'blocked') return '阻断：不能进入写前链'
  return '尚未生成 route receipt'
}

export function ContextHandoffPanel({ enabled, projectId, projectRevision = null, projectFingerprint = null }: ContextHandoffPanelProps) {
  const handoff = useContextHandoff(enabled, projectId, projectRevision, projectFingerprint)
  const [profile, setProfile] = useState('chapter')
  const [stage, setStage] = useState('draft')
  const [chapterId, setChapterId] = useState('CH001')
  const [taskIntent, setTaskIntent] = useState('把 O2-D 受控 distillation_index 入口装配成写前 Context Pack；不写正文。')
  const record = handoff.record
  const status = record?.status ?? 'awaiting_upstream'
  const copy = statusCopy[status]
  const entries = record?.upstream?.entries ?? []
  const canPrepare = Boolean(enabled && projectId && projectRevision && projectFingerprint && !handoff.busy)
  const canApprove = record?.status === 'prepared' && !handoff.busy
  const canRun = record?.status === 'approved' && !handoff.busy
  const canReject = record && ['prepared', 'approved', 'failed', 'rejected'].includes(record.status) && !handoff.busy
  const canDisable = record && record.status !== 'running' && record.status !== 'disabled' && !handoff.busy
  const selectedSummary = useMemo(() => {
    if (!record?.result?.selected_entries?.length) return '尚未生成 Context Pack'
    return record.result.selected_entries.map((entry) => entry.card_type).join(' / ')
  }, [record])

  return (
    <section className="context-handoff-panel" aria-label="受控入口到 Context Pack">
      <header className={`context-handoff-hero status-${status}`}>
        <span className="context-handoff-icon">
          {handoff.busy || status === 'running' || handoff.state === 'loading'
            ? <LoaderCircle size={16} className="spin" />
            : status === 'succeeded'
              ? <CheckCircle2 size={16} />
              : status === 'failed' || status === 'disabled' || status === 'rejected'
                ? <AlertTriangle size={16} />
                : <FileSearch size={16} />}
        </span>
        <div>
          <span className="panel-kicker">O2-E NLCL 相邻 handoff</span>
          <h3>{copy.title}</h3>
          <p>{copy.detail}</p>
        </div>
        <button type="button" className="icon-button" aria-label="刷新 Context handoff" onClick={() => void handoff.refresh()} disabled={!enabled || handoff.busy}>
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="context-handoff-flow" aria-label="受控入口到写前上下文包">
        <div><strong>O2-D distillation_index</strong><span>三类 active 入口、版本、hash</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>novel-kb-manager</strong><span>import-distillation-index → assemble-context</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>Runtime Context Pack</strong><span>只生成待审上下文，不写正文</span></div>
      </div>

      {handoff.error && <div className="context-handoff-error" role="alert"><AlertTriangle size={15} /><span>{handoff.error}</span></div>}

      {record?.result && status === 'succeeded' && (
        <div className="context-handoff-result" role="status" aria-label="Context Pack 已生成">
          <CheckCircle2 size={16} />
          <div>
            <strong>Context Pack 已生成，正文尚未写回</strong>
            <span>{record.result.profile ?? record.route.profile} / {record.result.stage ?? record.route.stage} / {record.result.chapter_id ?? record.route.chapter_id} · {selectedSummary}</span>
            <small>Context Pack 与 route receipt 已写入 Runtime；完整 id/hash 在技术详情中可复制。</small>
          </div>
        </div>
      )}

      <div className="context-handoff-author-summary" aria-label="作者可读结论">
        <div><BookOpenCheck size={15} /><strong>知识入口已受控</strong><span>{record?.upstream ? '三类 distillation_index 入口已绑定到当前项目快照。' : '等待 O2-D 入口'}</span></div>
        <div><Layers3 size={15} /><strong>只生成写前上下文</strong><span>正式正文尚未写回，project_truth_not_written=true。</span></div>
        <div><ShieldCheck size={15} /><strong>{routeHealthCopy(record?.result?.route_health)}</strong><span>route_health=ready 只说明可调用，不等于真实写作验证。</span></div>
      </div>

      <div className="context-handoff-route-editor" aria-label="assemble-context 路由参数">
        <label>
          <span>上下文档位</span>
          <input value={profile} onChange={(event) => setProfile(event.target.value)} disabled={handoff.busy} />
        </label>
        <label>
          <span>任务阶段</span>
          <input value={stage} onChange={(event) => setStage(event.target.value)} disabled={handoff.busy} />
        </label>
        <label>
          <span>章节</span>
          <input value={chapterId} onChange={(event) => setChapterId(event.target.value)} disabled={handoff.busy} />
        </label>
        <label className="wide">
          <span>任务意图</span>
          <textarea value={taskIntent} onChange={(event) => setTaskIntent(event.target.value)} disabled={handoff.busy} />
        </label>
      </div>

      {entries.length > 0 ? (
        <div className="context-handoff-entries" aria-label="将进入 Context Pack 的受控入口">
          {entries.map((entry) => (
            <article key={entry.card_id}>
              <span>{entryTypeLabel(entry.card_type)}</span>
              <strong>{entry.title}</strong>
              <p>{entry.route_terms.length ? entry.route_terms.join(' · ') : '缺路由词：需要补库后再运行'}</p>
            </article>
          ))}
        </div>
      ) : (
        <p className="context-handoff-empty">还没有可装配的 distillation_index。先在上方 O2-D 交接生成受控入口。</p>
      )}

      <div className="context-handoff-permissions" aria-label="权限与边界">
        <div><LockKeyhole size={14} /><strong>需要作者批准</strong><span>批准后才调用受管 adapter；拒绝、禁用、stale、篡改都会关闭。</span></div>
        <div><ShieldCheck size={14} /><strong>权限</strong><span>读取项目快照和 run staging；adapter 只写 staging；Runtime 负责持久化 Context Pack。</span></div>
      </div>

      <div className="context-handoff-actions">
        <button type="button" className="quiet-button" disabled={!canPrepare} onClick={() => void handoff.prepare({ profile, stage, chapterId, taskIntent })}>
          <SlidersHorizontal size={14} />准备 Context Pack
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
          <Play size={14} fill="currentColor" />运行 assemble-context
        </button>
      </div>

      {record && (
        <details className="context-handoff-tech">
          <summary>技术详情 / receipt / Context Pack</summary>
          <dl>
            <div><dt>context_handoff_id</dt><dd><code>{record.context_handoff_id}</code></dd></div>
            <div><dt>source handoff</dt><dd><code>{record.upstream?.source_handoff_id ?? 'none'}</code></dd></div>
            <div><dt>index hash</dt><dd><code>{fullEvidence(record.upstream?.distillation_index_hash)}</code></dd></div>
            <div><dt>workflow plan</dt><dd><code>{record.workflow?.workflow_plan_id ?? 'none'}</code></dd></div>
            <div><dt>skill receipt</dt><dd><code>{record.skill_receipt_id ?? 'none'}</code></dd></div>
            <div><dt>context pack</dt><dd><code>{record.context_pack_id ?? 'none'}</code></dd></div>
            <div><dt>context hash</dt><dd><code>{fullEvidence(record.context_pack_hash)}</code></dd></div>
            <div><dt>route receipt</dt><dd><code>{record.route_receipt_id ?? 'none'}</code></dd></div>
            <div><dt>route hash</dt><dd><code>{fullEvidence(record.route_receipt_hash)}</code></dd></div>
            <div><dt>adapter version</dt><dd><code>{record.activation?.skill_version ?? 'not active'}</code></dd></div>
          </dl>
        </details>
      )}
    </section>
  )
}

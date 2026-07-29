import { useState } from 'react'
import {
  AlertTriangle,
  BookCheck,
  BookDown,
  CheckCircle2,
  ChevronRight,
  CircleStop,
  Database,
  FileArchive,
  FileText,
  Inbox,
  LoaderCircle,
  Play,
  RefreshCw,
  RotateCcw,
  Search,
  ShieldCheck,
} from 'lucide-react'
import type { KnowledgeFormalizationRecord } from '../api/knowledge'
import {
  DISTILLATION_PREVIEW_TIMEOUT_MS,
  isRuntimeDistillationInvocationId,
  runDistillationInvocation,
  type DistillationInvocationStatus,
} from '../api/distillation'
import type {
  SourceImportExecutionReceiptRecord,
  SourceImportDistillationPrepareResult,
  SourceImportJob,
  SourceImportStatus,
  TomatoSearchCandidate,
} from '../api/sourceImports'
import { prepareSourceImportDistillationInvocation } from '../api/sourceImports'
import { RuntimeApiError } from '../api/runtime'
import {
  ACTIVE_SOURCE_IMPORT_STATUSES,
  type SourceImportLoadState,
  useSourceImports,
} from '../hooks/useSourceImports'
import { useDistillationInvocation } from '../hooks/useDistillationInvocation'

const statusLabels: Record<SourceImportStatus, string> = {
  queued: '排队中',
  downloading: '下载中',
  source_ready: '源包已校验',
  drafting: '生成草案',
  awaiting_review: '蒸馏草案待复核',
  formalizing: '正式化中',
  completed: '流程已完成',
  cancel_requested: '正在取消',
  cancelled: '已取消',
  failed: '失败',
  interrupted: '已中断',
}

const cancellableStatuses = new Set<SourceImportStatus>([
  'queued',
  'downloading',
  'source_ready',
  'drafting',
  'formalizing',
])

const retryableStatuses = new Set<SourceImportStatus>([
  'cancelled',
  'failed',
  'interrupted',
])

type RuntimeDistillationActionKind = 'prepare' | 'run'

interface RuntimeDistillationActionState {
  kind: RuntimeDistillationActionKind | null
  error: string | null
}

function compactTimestamp(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function runtimeActionErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
  }
  if (error instanceof Error) return error.message
  return 'Runtime 深蒸馏请求失败'
}

function progressPercent(job: SourceImportJob): number | null {
  if (job.progress.percent !== null) return job.progress.percent
  const { saved_chapters: saved, total_chapters: total } = job.progress
  if (saved === null || total === null || total <= 0) return null
  return Math.min(100, Math.max(0, Math.round((saved / total) * 100)))
}

function chapterProgress(job: SourceImportJob): string | null {
  const { saved_chapters: saved, total_chapters: total } = job.progress
  if (saved === null && total === null) return null
  return `${saved ?? 0} / ${total ?? '?'} 章`
}

function CandidateRow({
  candidate,
  disabled,
  onChoose,
}: {
  candidate: TomatoSearchCandidate
  disabled: boolean
  onChoose: () => void
}) {
  return (
    <button
      type="button"
      className="tomato-candidate"
      aria-label={`选择《${candidate.title}》，作者 ${candidate.author}`}
      disabled={disabled}
      onClick={onChoose}
    >
      <span className="candidate-index">{candidate.index}</span>
      <span className="candidate-copy">
        <strong>{candidate.title}</strong>
        <small>{candidate.author} · ID {candidate.book_id}</small>
      </span>
      <ChevronRight size={14} />
    </button>
  )
}

const invocationStatusLabels: Record<DistillationInvocationStatus, string> = {
  prepared: '已准备',
  running: 'Provider 执行中',
  output_persisted: '候选已持久化',
  interrupted: '等待恢复决策',
  succeeded: '已成功',
  failed: '失败',
  cancelled: '已取消',
  abandoned: '已放弃',
}

function RuntimeDistillationEvidence({
  invocationId,
  prepareResult = null,
  actionState = null,
  onPrepare,
  onRun,
}: {
  invocationId: string | null
  prepareResult?: SourceImportDistillationPrepareResult | null
  actionState?: RuntimeDistillationActionState | null
  onPrepare?: () => void
  onRun?: () => void
}) {
  const preparedSnapshot = prepareResult?.snapshot ?? null
  const effectiveInvocationId = preparedSnapshot?.invocation.invocation_id ?? invocationId
  const isRuntimeId = isRuntimeDistillationInvocationId(effectiveInvocationId)
  const runtimeInvocation = useDistillationInvocation(effectiveInvocationId, isRuntimeId)
  const prepareBusy = actionState?.kind === 'prepare'
  const runBusy = actionState?.kind === 'run'

  if (!effectiveInvocationId) return null

  if (!isRuntimeId) {
    return (
      <section className="source-packet-summary deep-distillation-evidence" aria-label="Runtime 深蒸馏 Invocation">
        <div className="source-packet-heading">
          <AlertTriangle size={15} />
          <strong>Runtime 深蒸馏 Invocation</strong>
          <span>未创建</span>
        </div>
        <div className="distillation-boundary boundary-review" role="status">
          <AlertTriangle size={15} />
          <div>
            <strong>仍是旧 Source Import Skill 草案</strong>
            <span>这个收据里的 distillation_invocation_id 不是 Runtime-owned deep invocation，不能把它当作 O2 ledger / 三候选闭环完成。</span>
            {onPrepare && (
              <button
                type="button"
                className="review-draft-button"
                disabled={prepareBusy}
                onClick={onPrepare}
              >
                {prepareBusy ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />}
                {prepareBusy ? '准备中' : '准备 Runtime 深蒸馏'}
              </button>
            )}
          </div>
        </div>
        {actionState?.error && (
          <div className="source-import-error" role="alert">
            <AlertTriangle size={14} />
            <span>{actionState.error}</span>
          </div>
        )}
        <code title={effectiveInvocationId}>legacy_distillation_invocation_id · {effectiveInvocationId}</code>
      </section>
    )
  }

  const snapshot = preparedSnapshot
    && (!runtimeInvocation.snapshot
      || preparedSnapshot.invocation.state_revision >= runtimeInvocation.snapshot.invocation.state_revision)
    ? preparedSnapshot
    : runtimeInvocation.snapshot
  if (!snapshot && (runtimeInvocation.state === 'idle' || runtimeInvocation.state === 'loading')) {
    return (
      <section className="source-packet-summary deep-distillation-evidence" aria-label="Runtime 深蒸馏 Invocation">
        <div className="source-packet-heading">
          <LoaderCircle size={15} className="spin" />
          <strong>Runtime 深蒸馏 Invocation</strong>
          <span>读取中</span>
        </div>
        <div className="source-search-notice">
          <LoaderCircle size={14} className="spin" />
          <span>正在读取 Runtime-owned ledger、Provider turn 与三候选状态</span>
        </div>
      </section>
    )
  }

  if (!snapshot && runtimeInvocation.state === 'missing') {
    return (
      <section className="source-packet-summary deep-distillation-evidence" aria-label="Runtime 深蒸馏 Invocation">
        <div className="source-packet-heading">
          <AlertTriangle size={15} />
          <strong>Runtime 深蒸馏 Invocation</strong>
          <span>未找到</span>
          <button type="button" className="icon-button" aria-label="重新读取 Runtime 深蒸馏 Invocation" onClick={runtimeInvocation.refresh}><RefreshCw size={13} /></button>
        </div>
        <div className="distillation-boundary boundary-review" role="status">
          <AlertTriangle size={15} />
          <div>
            <strong>来源收据指向的 deep invocation 不在 Runtime store</strong>
            <span>当前不能确认 provider_dispatch、candidate_output 或 recovery dossier。</span>
            {onPrepare && (
              <button
                type="button"
                className="review-draft-button"
                disabled={prepareBusy}
                onClick={onPrepare}
              >
                {prepareBusy ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />}
                {prepareBusy ? '准备中' : '重新准备来源锚点'}
              </button>
            )}
          </div>
        </div>
        <code title={effectiveInvocationId}>invocation_id · {effectiveInvocationId}</code>
      </section>
    )
  }

  if (!snapshot && runtimeInvocation.state === 'error') {
    return (
      <section className="source-packet-summary deep-distillation-evidence" aria-label="Runtime 深蒸馏 Invocation">
        <div className="source-packet-heading">
          <AlertTriangle size={15} />
          <strong>Runtime 深蒸馏 Invocation</strong>
          <span>读取失败</span>
          <button type="button" className="icon-button" aria-label="重试读取 Runtime 深蒸馏 Invocation" onClick={runtimeInvocation.refresh}><RefreshCw size={13} /></button>
        </div>
        <div className="source-import-error" role="alert">
          <AlertTriangle size={14} />
          <span>{runtimeInvocation.error ?? 'Runtime 返回了无效的 deep invocation snapshot'}</span>
        </div>
      </section>
    )
  }

  if (!snapshot) return null
  const head = snapshot.invocation
  const candidateCounts = `${head.work_count ?? 0}/${head.voice_count ?? 0}/${head.validator_count ?? 0}`
  const recoveryLabel = head.recovery_dossier_receipt_id
    ? head.recovery_decision_receipt_id
      ? '已决议'
      : '待决策'
    : '无恢复'
  const statusLabel = invocationStatusLabels[head.status]

  return (
    <section className="source-packet-summary deep-distillation-evidence" aria-label="Runtime 深蒸馏 Invocation">
      <div className="source-packet-heading">
        <ShieldCheck size={15} />
        <strong>Runtime 深蒸馏 Invocation</strong>
        <span>{statusLabel}</span>
        <button type="button" className="icon-button" aria-label="刷新 Runtime 深蒸馏 Invocation" onClick={runtimeInvocation.refresh}><RefreshCw size={13} /></button>
      </div>
      <dl>
        <div><dt>ledger</dt><dd>{snapshot.receipts.length} 条</dd></div>
        <div><dt>Provider</dt><dd>{head.provider_turn_count} turn</dd></div>
        <div><dt>三候选</dt><dd>{candidateCounts}</dd></div>
        <div><dt>重放</dt><dd>{head.provider_replay_count} 次</dd></div>
        <div><dt>revision</dt><dd>{head.state_revision}</dd></div>
        <div><dt>恢复</dt><dd>{recoveryLabel}</dd></div>
      </dl>
      <code title={head.invocation_id}>invocation_id · {head.invocation_id}</code>
      <code title={head.head_hash}>head_hash · {head.head_hash}</code>
      <code title={head.last_receipt_hash}>last_receipt_hash · {head.last_receipt_hash}</code>
      {actionState?.error && (
        <div className="source-import-error" role="alert">
          <AlertTriangle size={14} />
          <span>{actionState.error}</span>
        </div>
      )}
      {head.status === 'prepared' && onRun && prepareResult && (
        <button
          type="button"
          className="review-draft-button"
          disabled={runBusy}
          onClick={onRun}
        >
          {runBusy ? <LoaderCircle size={13} className="spin" /> : <Play size={13} fill="currentColor" />}
          {runBusy ? '运行中' : '运行 Provider 深蒸馏'}
        </button>
      )}
      {head.status === 'prepared' && onPrepare && !prepareResult && (
        <button
          type="button"
          className="review-draft-button"
          disabled={prepareBusy}
          onClick={onPrepare}
        >
          {prepareBusy ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />}
          {prepareBusy ? '准备中' : '重新准备来源锚点'}
        </button>
      )}
      {head.candidate_output_receipt_id && (
        <code title={head.candidate_output_receipt_id}>candidate_output_receipt_id · {head.candidate_output_receipt_id}</code>
      )}
      {head.recovery_dossier_receipt_id && (
        <code title={head.recovery_dossier_receipt_id}>recovery_dossier_receipt_id · {head.recovery_dossier_receipt_id}</code>
      )}
    </section>
  )
}

function ImportReceiptEvidence({
  state,
  executionReceipt,
  formalization,
  error,
  runtimePrepareResult,
  runtimeActionState,
  onPrepareRuntime,
  onRunRuntime,
}: {
  state: SourceImportLoadState
  executionReceipt: SourceImportExecutionReceiptRecord | null
  formalization: KnowledgeFormalizationRecord | null
  error: string | null
  runtimePrepareResult?: SourceImportDistillationPrepareResult | null
  runtimeActionState?: RuntimeDistillationActionState | null
  onPrepareRuntime?: () => void
  onRunRuntime?: () => void
}) {
  const receipt = executionReceipt?.receipt ?? null
  const formalizationReceipt = formalization?.receipt ?? null
  const lineageLabel = executionReceipt?.lineage_status
    ?? (state === 'loading' ? '读取中' : '未验证')

  return (
    <section className="source-packet-summary" aria-label="执行与正式化收据">
      <div className="source-packet-heading">
        <ShieldCheck size={15} />
        <strong>执行与正式化收据</strong>
        <span>{lineageLabel}</span>
      </div>

      {(state === 'idle' || state === 'loading') && (
        <div className="source-search-notice">
          <LoaderCircle size={14} className="spin" />
          <span>正在读取 Runtime 收据</span>
        </div>
      )}

      {error && (
        <div className="source-import-error" role="alert">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      )}

      {receipt ? (
        <>
          <dl>
            <div><dt>lineage</dt><dd>{executionReceipt?.lineage_status}</dd></div>
            <div><dt>revision</dt><dd>{receipt.revision}</dd></div>
            <div><dt>候选卡</dt><dd>{receipt.card_count}</dd></div>
          </dl>
          <code title={receipt.receipt_id}>receipt_id · {receipt.receipt_id}</code>
          <code title={receipt.receipt_hash}>receipt_hash · {receipt.receipt_hash}</code>
          <code title={receipt.run_id}>run_id · {receipt.run_id}</code>
          <code title={receipt.idempotency_key}>idempotency_key · {receipt.idempotency_key}</code>
        </>
      ) : state === 'ready' && executionReceipt?.lineage_status === 'legacy_unverified' ? (
        <div className="distillation-boundary boundary-review" role="status">
          <AlertTriangle size={15} />
          <div>
            <strong>legacy_unverified</strong>
            <span>没有可验证的 Source Import 执行收据，不能确认该批次已正式入库。</span>
          </div>
        </div>
      ) : null}

      {formalization && (
        <>
          <dl>
            <div><dt>formalization</dt><dd>{formalization.status}</dd></div>
            <div><dt>receipt</dt><dd>{formalizationReceipt ? 'verified' : '未发布'}</dd></div>
            <div><dt>source</dt><dd>{formalization.source_import_id.slice(-8)}</dd></div>
          </dl>
          {formalizationReceipt && (
            <>
              <code title={formalizationReceipt.formalization_receipt_id}>formalization_receipt_id · {formalizationReceipt.formalization_receipt_id}</code>
              <code title={formalizationReceipt.receipt_hash}>formalization_receipt_hash · {formalizationReceipt.receipt_hash}</code>
            </>
          )}
        </>
      )}

      <RuntimeDistillationEvidence
        invocationId={receipt?.distillation_invocation_id ?? null}
        prepareResult={runtimePrepareResult}
        actionState={runtimeActionState}
        onPrepare={onPrepareRuntime}
        onRun={onRunRuntime}
      />
    </section>
  )
}

function ImportDetail({
  job,
  evidenceState,
  executionReceipt,
  formalization,
  evidenceError,
  runtimePrepareResult,
  runtimeActionState,
  onPrepareRuntime,
  onRunRuntime,
  onReviewDraft,
}: {
  job: SourceImportJob
  evidenceState: SourceImportLoadState
  executionReceipt: SourceImportExecutionReceiptRecord | null
  formalization: KnowledgeFormalizationRecord | null
  evidenceError: string | null
  runtimePrepareResult?: SourceImportDistillationPrepareResult | null
  runtimeActionState?: RuntimeDistillationActionState | null
  onPrepareRuntime?: () => void
  onRunRuntime?: () => void
  onReviewDraft?: (sourceImportId: string) => void
}) {
  const percent = progressPercent(job)
  const chapters = chapterProgress(job)
  const isActive = ACTIVE_SOURCE_IMPORT_STATUSES.has(job.status)
  const isVerifiedCompletion = Boolean(
    executionReceipt?.lineage_status === 'verified'
    && executionReceipt.receipt
    && formalization?.status === 'verified'
    && formalization.receipt,
  )
  const isLegacyCompletion = evidenceState === 'ready' && (
    executionReceipt?.lineage_status === 'legacy_unverified'
    || formalization?.status === 'legacy_unverified'
  )
  const completionTitle = isVerifiedCompletion
    ? '正式蒸馏已验证入库'
    : isLegacyCompletion
      ? 'legacy_unverified'
      : evidenceState === 'error'
        ? '正式入库收据读取失败'
        : '正式入库尚未验证'
  const completionMessage = isVerifiedCompletion
    ? '正式卡、来源执行收据与正式化收据已绑定'
    : isLegacyCompletion
      ? '缺少可验证收据，不能把流程完成状态当作正式入库证明。'
      : evidenceState === 'error'
        ? '无法读取完整收据链，当前不能确认正式入库。'
        : '正在核验来源执行与知识正式化收据。'

  return (
    <div className="source-import-detail">
      <header className="source-import-detail-header">
        <div className="source-import-title-line">
          <span className={`source-import-status status-${job.status}`}>{statusLabels[job.status]}</span>
          <span>{compactTimestamp(job.updated_at)}</span>
        </div>
        <h3>{job.candidate.title}</h3>
        <p>{job.provider === 'tomato'
          ? `${job.candidate.author} · 番茄 ID ${job.candidate.book_id}`
          : `${job.candidate.author} · 本地 ${job.candidate.source_format.toUpperCase()}`}</p>
      </header>

      <section className="import-progress" aria-label="资料导入进度">
        <div className="import-progress-copy">
          <span className="import-progress-icon">
            {isActive ? <LoaderCircle size={15} className="spin" /> : <BookCheck size={15} />}
          </span>
          <div><strong>{job.progress.message}</strong><span>{chapters ?? job.progress.stage}</span></div>
          {percent !== null && <b>{percent}%</b>}
        </div>
        {percent !== null && <div className="import-progress-track"><i style={{ width: `${percent}%` }} /></div>}
      </section>

      {job.status === 'awaiting_review' && (
        <section className="distillation-boundary boundary-review" role="status">
          <AlertTriangle size={16} />
          <div>
            <strong>蒸馏草案待复核</strong>
            <span>{job.distillation_draft?.card_count ?? 0} 张候选卡，尚未进入正式知识库</span>
            {onReviewDraft && <button type="button" className="review-draft-button" onClick={() => onReviewDraft(job.import_id)}><Database size={13} />复核候选</button>}
          </div>
        </section>
      )}

      {job.status === 'completed' && (
        <section className={`distillation-boundary ${isVerifiedCompletion ? 'boundary-completed' : 'boundary-review'}`} role="status">
          {isVerifiedCompletion ? <CheckCircle2 size={16} /> : evidenceState === 'loading' || evidenceState === 'idle' ? <LoaderCircle size={16} className="spin" /> : <AlertTriangle size={16} />}
          <div>
            <strong>{completionTitle}</strong>
            <span>{completionMessage}</span>
            {onReviewDraft && <button type="button" className="review-draft-button" onClick={() => onReviewDraft(job.import_id)}><Database size={13} />查看知识批次</button>}
          </div>
        </section>
      )}

      {job.status === 'formalizing' && (
        <section className="distillation-boundary boundary-review" role="status">
          <LoaderCircle size={16} className="spin" />
          <div><strong>正式化处理中</strong><span>正式化收据发布前，不视为正式入库。</span></div>
        </section>
      )}

      {(['awaiting_review', 'formalizing', 'completed'] as SourceImportStatus[]).includes(job.status) && (
        <ImportReceiptEvidence
          state={evidenceState}
          executionReceipt={executionReceipt}
          formalization={formalization}
          error={evidenceError}
          runtimePrepareResult={runtimePrepareResult}
          runtimeActionState={runtimeActionState}
          onPrepareRuntime={onPrepareRuntime}
          onRunRuntime={onRunRuntime}
        />
      )}

      {job.error && (
        <section className="source-import-error" role="alert">
          <AlertTriangle size={15} />
          <span>{job.error}</span>
        </section>
      )}

      {job.source_packet && (
        <section className="source-packet-summary">
          <div className="source-packet-heading"><FileArchive size={15} /><strong>完整源包</strong><span>{job.source_packet.download_scope}</span></div>
          <dl>
            <div><dt>正文</dt><dd>{job.source_packet.char_count.toLocaleString()} 字</dd></div>
            <div><dt>章节标记</dt><dd>{job.source_packet.chapter_marker_count.toLocaleString()}</dd></div>
            <div><dt>产物</dt><dd>{job.source_packet.artifacts.length}</dd></div>
          </dl>
          {'source_file_sha256' in job.source_packet && (
            <code title={job.source_packet.source_file_sha256}>source_sha256 · {job.source_packet.source_file_sha256}</code>
          )}
        </section>
      )}
    </div>
  )
}

export function TomatoImportPanel({
  enabled = true,
  projectId = null,
  projectRevision = null,
  projectFingerprint = null,
  onReviewDraft,
}: {
  enabled?: boolean
  projectId?: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  onReviewDraft?: (sourceImportId: string) => void
}) {
  const imports = useSourceImports(enabled, projectId)
  const [sourceMode, setSourceMode] = useState<'tomato' | 'local'>('tomato')
  const [runtimePrepareResults, setRuntimePrepareResults] = useState<Record<string, SourceImportDistillationPrepareResult>>({})
  const [runtimeActions, setRuntimeActions] = useState<Record<string, RuntimeDistillationActionState>>({})
  const busySearch = imports.searchState === 'searching' || imports.searchState === 'creating'
  const busyLocal = imports.localState === 'inspecting' || imports.localState === 'creating'
  const canCancel = imports.selected
    ? cancellableStatuses.has(imports.selected.status)
    : false
  const canRetry = imports.selected
    ? retryableStatuses.has(imports.selected.status)
    : false

  const setRuntimeAction = (
    sourceImportId: string,
    state: RuntimeDistillationActionState,
  ) => {
    setRuntimeActions((current) => ({ ...current, [sourceImportId]: state }))
  }

  const prepareRuntimeDistillation = async (sourceImportId: string) => {
    if (!projectId) {
      setRuntimeAction(sourceImportId, {
        kind: null,
        error: '请先读取一个项目，再准备 Runtime 深蒸馏。',
      })
      return
    }
    setRuntimeAction(sourceImportId, { kind: 'prepare', error: null })
    try {
      const result = await prepareSourceImportDistillationInvocation(
        sourceImportId,
        {
          project_id: projectId,
          expected_project_revision: projectRevision ?? undefined,
          expected_project_fingerprint: projectFingerprint ?? undefined,
        },
        { timeoutMs: DISTILLATION_PREVIEW_TIMEOUT_MS },
      )
      setRuntimePrepareResults((current) => ({
        ...current,
        [sourceImportId]: result,
      }))
      setRuntimeAction(sourceImportId, { kind: null, error: null })
      imports.refresh()
    } catch (error) {
      setRuntimeAction(sourceImportId, {
        kind: null,
        error: runtimeActionErrorMessage(error),
      })
    }
  }

  const runPreparedRuntimeDistillation = async (sourceImportId: string) => {
    const prepared = runtimePrepareResults[sourceImportId]
    if (!prepared) {
      setRuntimeAction(sourceImportId, {
        kind: null,
        error: '请先准备 Runtime 深蒸馏来源锚点，再运行 Provider。',
      })
      return
    }
    const invocationId = prepared.snapshot.invocation.invocation_id
    setRuntimeAction(sourceImportId, { kind: 'run', error: null })
    try {
      const result = await runDistillationInvocation(
        invocationId,
        {
          anchor_catalog: prepared.anchor_catalog,
          text_by_anchor_id: prepared.text_by_anchor_id,
        },
        { timeoutMs: DISTILLATION_PREVIEW_TIMEOUT_MS },
      )
      setRuntimePrepareResults((current) => ({
        ...current,
        [sourceImportId]: {
          ...prepared,
          snapshot: result,
        },
      }))
      setRuntimeAction(sourceImportId, { kind: null, error: null })
      imports.refresh()
    } catch (error) {
      setRuntimeAction(sourceImportId, {
        kind: null,
        error: runtimeActionErrorMessage(error),
      })
    }
  }

  if (!enabled) {
    return (
      <div className="inspector-body source-import-panel source-import-panel-offline">
        <div className="source-import-empty"><AlertTriangle size={17} /><span>Writing Runtime 未就绪</span></div>
      </div>
    )
  }

  return (
    <div className="inspector-body source-import-panel">
      <div className="source-mode-switch" role="group" aria-label="资料来源">
        <button type="button" aria-pressed={sourceMode === 'tomato'} onClick={() => setSourceMode('tomato')}><BookDown size={14} />番茄</button>
        <button type="button" aria-pressed={sourceMode === 'local'} onClick={() => setSourceMode('local')}><FileArchive size={14} />本地 TXT / EPUB</button>
      </div>

      {sourceMode === 'tomato' ? (
        <form
          className="tomato-search-form"
          onSubmit={(event) => {
            event.preventDefault()
            void imports.search()
          }}
        >
          <label htmlFor="tomato-title-query">番茄小说</label>
          <div className="tomato-search-control">
            <Search size={15} />
            <input
              id="tomato-title-query"
              type="search"
              autoComplete="off"
              maxLength={500}
              placeholder="输入小说名"
              value={imports.query}
              disabled={busySearch}
              onChange={(event) => imports.setQuery(event.target.value)}
            />
            <button type="submit" disabled={!imports.query.trim() || busySearch}>
              {busySearch ? <LoaderCircle size={14} className="spin" /> : <BookDown size={14} />}
              {imports.searchState === 'searching' ? '检索中' : imports.searchState === 'creating' ? '创建中' : '下载并蒸馏'}
            </button>
          </div>
        </form>
      ) : (
        <form
          className="tomato-search-form local-source-form"
          onSubmit={(event) => {
            event.preventDefault()
            void imports.importLocalSource()
          }}
        >
          <label htmlFor="local-source-path">本地 TXT / EPUB</label>
          <div className="tomato-search-control">
            <FileText size={15} />
            <input
              id="local-source-path"
              type="text"
              autoComplete="off"
              maxLength={4096}
              placeholder="输入 .txt 或 .epub 文件路径"
              value={imports.localPath}
              disabled={busyLocal}
              onChange={(event) => imports.setLocalPath(event.target.value)}
            />
            <button type="submit" disabled={!imports.localPath.trim() || busyLocal}>
              {busyLocal ? <LoaderCircle size={14} className="spin" /> : <FileText size={14} />}
              {imports.localState === 'inspecting' ? '校验中' : imports.localState === 'creating' ? '创建中' : '校验并蒸馏'}
            </button>
          </div>
        </form>
      )}

      <div className="source-search-result" aria-live="polite">
        {sourceMode === 'tomato' && imports.searchState === 'searching' && (
          <div className="source-search-notice"><LoaderCircle size={15} className="spin" /><span>正在检索“{imports.searchQuery}”</span></div>
        )}
        {sourceMode === 'tomato' && imports.searchState === 'creating' && (
          <div className="source-search-notice"><LoaderCircle size={15} className="spin" /><span>精确书名已匹配，正在创建导入任务</span></div>
        )}
        {sourceMode === 'tomato' && imports.searchState === 'created' && (
          <div className="source-search-notice is-success"><CheckCircle2 size={15} /><span>导入任务已创建</span></div>
        )}
        {sourceMode === 'tomato' && imports.searchState === 'not_found' && (
          <div className="source-search-notice is-empty"><Inbox size={15} /><span>没有找到“{imports.searchQuery}”</span></div>
        )}
        {sourceMode === 'tomato' && imports.searchError && (
          <div className="source-search-notice is-error" role="alert"><AlertTriangle size={15} /><span>{imports.searchError}</span></div>
        )}
        {sourceMode === 'tomato' && imports.searchState === 'selection_required' && (
          <div className="tomato-candidates">
            <div className="tomato-candidates-heading"><strong>选择同名作品</strong><span>{imports.candidates.length} 个结果</span></div>
            {imports.candidates.map((candidate) => (
              <CandidateRow
                key={candidate.book_id}
                candidate={candidate}
                disabled={busySearch}
                onChoose={() => void imports.chooseCandidate(candidate)}
              />
            ))}
          </div>
        )}
        {sourceMode === 'local' && imports.localState === 'inspecting' && (
          <div className="source-search-notice"><LoaderCircle size={15} className="spin" /><span>正在校验本地 TXT / EPUB</span></div>
        )}
        {sourceMode === 'local' && imports.localState === 'creating' && (
          <div className="source-search-notice"><LoaderCircle size={15} className="spin" /><span>来源已锁定，正在创建导入任务</span></div>
        )}
        {sourceMode === 'local' && imports.localState === 'created' && (
          <div className="source-search-notice is-success"><CheckCircle2 size={15} /><span>本地导入任务已创建</span></div>
        )}
        {sourceMode === 'local' && imports.localCandidate && (
          <div className="local-source-identity">
            <strong>{imports.localCandidate.title}</strong>
            <span>{imports.localCandidate.source_format.toUpperCase()} · {imports.localCandidate.source_size_bytes.toLocaleString()} bytes</span>
            <code title={imports.localCandidate.source_sha256}>{imports.localCandidate.source_sha256}</code>
          </div>
        )}
        {sourceMode === 'local' && imports.localError && (
          <div className="source-search-notice is-error" role="alert"><AlertTriangle size={15} /><span>{imports.localError}</span></div>
        )}
      </div>

      <div className="source-import-list-header">
        <div><strong>导入任务</strong><span>{imports.hasActiveJobs ? '自动同步中' : `${imports.jobs.length} 条记录`}</span></div>
        <button type="button" className="icon-button" aria-label="刷新导入任务" title="刷新导入任务" onClick={imports.refresh}><RefreshCw size={14} /></button>
      </div>

      {imports.listError && (
        <div className="source-import-list-error" role="alert">
          <AlertTriangle size={14} /><span>{imports.listError}</span>
          <button type="button" className="icon-button" aria-label="重试读取导入任务" onClick={imports.refresh}><RefreshCw size={13} /></button>
        </div>
      )}

      {imports.listState === 'loading' && imports.jobs.length === 0 ? (
        <div className="source-import-empty"><LoaderCircle size={17} className="spin" /><span>正在读取导入任务</span></div>
      ) : imports.listState === 'ready' && imports.jobs.length === 0 ? (
        <div className="source-import-empty"><Inbox size={17} /><span>暂无导入任务</span></div>
      ) : (
        <div className="source-import-list" aria-label="资料导入任务">
          {imports.jobs.map((job) => (
            <button
              type="button"
              className={`source-import-row${imports.selectedId === job.import_id ? ' is-active' : ''}`}
              aria-pressed={imports.selectedId === job.import_id}
              key={job.import_id}
              onClick={() => imports.select(job.import_id)}
            >
              <span className={`source-import-status status-${job.status}`}>{statusLabels[job.status]}</span>
              <span className="source-import-row-copy"><strong>{job.candidate.title}</strong><small>{job.provider === 'local_file' ? job.candidate.source_format.toUpperCase() : '番茄'} · {job.candidate.author} · {compactTimestamp(job.updated_at)}</small></span>
              <ChevronRight size={13} />
            </button>
          ))}
        </div>
      )}

      <div className="source-import-detail-slot">
        {imports.selected ? (
          <ImportDetail
            job={imports.selected}
            evidenceState={imports.evidenceState}
            executionReceipt={imports.executionReceipt}
            formalization={imports.formalization}
            evidenceError={imports.evidenceError}
            runtimePrepareResult={runtimePrepareResults[imports.selected.import_id] ?? null}
            runtimeActionState={runtimeActions[imports.selected.import_id] ?? null}
            onPrepareRuntime={() => void prepareRuntimeDistillation(imports.selected!.import_id)}
            onRunRuntime={() => void runPreparedRuntimeDistillation(imports.selected!.import_id)}
            onReviewDraft={onReviewDraft}
          />
        ) : imports.jobs.length > 0 ? (
          <div className="source-import-empty"><Inbox size={17} /><span>选择一条导入任务</span></div>
        ) : null}
      </div>

      {(canCancel || canRetry || imports.actionError) && (
        <footer className="source-import-actions">
          {imports.actionError && <div className="source-import-action-error" role="alert"><AlertTriangle size={13} /><span>{imports.actionError}</span></div>}
          {canCancel && (
            <button type="button" className="source-import-cancel" disabled={imports.action !== null} onClick={() => void imports.cancel()}>
              {imports.action?.kind === 'cancel' ? <LoaderCircle size={14} className="spin" /> : <CircleStop size={14} />}
              取消任务
            </button>
          )}
          {canRetry && (
            <button type="button" className="source-import-retry" disabled={imports.action !== null} onClick={() => void imports.retry()}>
              {imports.action?.kind === 'retry' ? <LoaderCircle size={14} className="spin" /> : <RotateCcw size={14} />}
              重试任务
            </button>
          )}
        </footer>
      )}
    </div>
  )
}

import {
  AlertTriangle,
  BookOpenText,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  createReaderReview,
  decideReaderReview,
  getReaderReview,
  getReaderReviewDecision,
  type ChangeSet,
  type ReaderFindingDecisionValue,
  type ReaderReviewDecisionReceipt,
  type ReaderReviewReceipt,
} from '../api/changeSets'
import { RuntimeApiError } from '../api/runtime'

function humanRuntimeError(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: string; code?: string }
      detail?: string
    } | null
    return payload?.error?.message
      ?? payload?.detail
      ?? payload?.error?.code
      ?? `Runtime HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '未知错误'
}

function severityClass(severity: string): string {
  if (severity === 'concern') return 'fail'
  if (severity === 'watch') return 'warn'
  return 'pass'
}

export function ReaderWorkbenchPanel({
  enabled,
  detail,
  onOpenChanges,
}: {
  enabled: boolean
  detail: ChangeSet | null
  onOpenChanges?: () => void
}) {
  const [receipt, setReceipt] = useState<ReaderReviewReceipt | null>(null)
  const [decision, setDecision] = useState<ReaderReviewDecisionReceipt | null>(null)
  const [localDecisions, setLocalDecisions] = useState<Record<string, ReaderFindingDecisionValue>>({})
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const active = detail != null && (detail.status === 'applied' || detail.status === 'rolled_back')
  const fingerprint = detail?.writeback?.result_fingerprint
    ?? detail?.rollback?.result_fingerprint
    ?? null
  const shortId = useMemo(
    () => (detail ? `${detail.change_set_id.slice(0, 10)}…` : '未选择'),
    [detail],
  )

  const load = useCallback(async () => {
    if (!enabled || !active || !detail) {
      setReceipt(null)
      setDecision(null)
      setLocalDecisions({})
      setError(null)
      return
    }
    setBusy(true)
    setError(null)
    try {
      const [review, decisionResponse] = await Promise.all([
        getReaderReview(detail.change_set_id),
        getReaderReviewDecision(detail.change_set_id),
      ])
      setReceipt(review.receipt)
      setDecision(decisionResponse.receipt)
      const next: Record<string, ReaderFindingDecisionValue> = {}
      for (const item of decisionResponse.receipt?.decisions ?? []) {
        next[item.finding_id] = item.decision
      }
      setLocalDecisions(next)
    } catch (currentError) {
      setReceipt(null)
      setDecision(null)
      setLocalDecisions({})
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [active, detail, enabled])

  useEffect(() => {
    void load()
  }, [load])

  const runReview = useCallback(async () => {
    if (!enabled || !detail || !fingerprint) return
    setBusy(true)
    setError(null)
    try {
      const response = await createReaderReview(detail.change_set_id, fingerprint)
      setReceipt(response.receipt)
      setDecision(null)
      setLocalDecisions({})
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [detail, enabled, fingerprint])

  const finalizeDecisions = useCallback(async () => {
    if (!detail || !receipt) return
    const decisions = receipt.findings.map((finding) => ({
      finding_id: finding.finding_id,
      decision: localDecisions[finding.finding_id] ?? 'rejected',
    }))
    setBusy(true)
    setError(null)
    try {
      const response = await decideReaderReview(
        detail.change_set_id,
        receipt.receipt_hash,
        decisions,
        true,
      )
      setDecision(response.receipt)
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [detail, localDecisions, receipt])

  return (
    <section className="reader-workbench-panel" aria-label="Reader 工作面" aria-busy={busy}>
      <header className="reader-workbench-hero">
        <span className="reader-workbench-icon">
          {busy ? <LoaderCircle size={16} className="spin" /> : <BookOpenText size={16} />}
        </span>
        <div>
          <span className="panel-kicker">FR-READ · 冷读工作面</span>
          <h3>
            {decision
              ? '冷读决策已落档'
              : receipt
                ? '冷读意见待作者决策'
                : active
                  ? '对已落盘结果做冷读'
                  : '需要 applied / rolled_back ChangeSet'}
          </h3>
          <p>
            ChangeSet <code>{shortId}</code>
            {detail ? ` · status=${detail.status}` : ''}
            。隔离意见，不覆盖 Gate，不宣称 quality_assessed，不解锁 formal draft。
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="运行冷读 Reader"
          disabled={!enabled || !active || !fingerprint || busy}
          onClick={() => void runReview()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="reader-workbench-flags" aria-label="Reader 边界">
        <span>overrides_gate=false</span>
        <span>quality_assessed=false</span>
        <span>formal_draft_allowed=false</span>
        <span>mode=deterministic_cold_read_v1</span>
      </div>

      {!enabled && (
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>Writing Runtime 未就绪</span>
        </div>
      )}
      {enabled && !detail && (
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>请先在「改动」页选择 ChangeSet</span>
        </div>
      )}
      {enabled && detail && !active && (
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>冷读仅对 applied / rolled_back 结果开放；当前 status={detail.status}</span>
        </div>
      )}
      {error && (
        <div className="agent-inline-error" role="alert">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      )}

      {receipt && (
        <>
          <div className="gate-summary reader-workbench-summary">
            <ShieldCheck size={19} />
            <div>
              <strong>
                {decision
                  ? `已决策 · follow-up ${decision.follow_up_change_set_id ?? 'none'}`
                  : `发现 ${receipt.findings.length} 条冷读意见`}
              </strong>
              <span>
                outcome={receipt.outcome} · rev={receipt.result_revision} · fingerprint=
                {receipt.result_fingerprint.slice(0, 12)}…
              </span>
            </div>
          </div>

          <section>
            <span className="section-label">冷读发现</span>
            <div className="gate-list">
              {receipt.findings.length === 0 && (
                <div className="gate-row gate-pass">
                  <span className="gate-level">OK</span>
                  <span className="gate-icon"><CheckCircle2 size={15} /></span>
                  <div>
                    <strong>无结构冷读风险</strong>
                    <span>仍不宣称文学质量已评估</span>
                  </div>
                </div>
              )}
              {receipt.findings.map((finding) => (
                <div
                  className={`gate-row gate-${severityClass(finding.severity)}`}
                  key={finding.finding_id}
                >
                  <span className="gate-level">{finding.severity.toUpperCase()}</span>
                  <span className="gate-icon">
                    {finding.severity === 'info'
                      ? <CheckCircle2 size={15} />
                      : <AlertTriangle size={15} />}
                  </span>
                  <div>
                    <strong>{finding.kind}</strong>
                    <span>
                      {finding.summary}
                      {finding.relative_path ? ` · ${finding.relative_path}` : ''}
                    </span>
                    {finding.evidence_quote && (
                      <em className="reader-evidence-quote">{finding.evidence_quote}</em>
                    )}
                    {!decision && (
                      <label className="reader-decision-select">
                        <span>作者决策</span>
                        <select
                          aria-label={`决策 ${finding.finding_id}`}
                          value={localDecisions[finding.finding_id] ?? 'rejected'}
                          disabled={busy}
                          onChange={(event) => {
                            const value = event.target.value as ReaderFindingDecisionValue
                            setLocalDecisions((current) => ({
                              ...current,
                              [finding.finding_id]: value,
                            }))
                          }}
                        >
                          <option value="adopted">采纳</option>
                          <option value="rejected">拒绝</option>
                          <option value="deferred">暂缓</option>
                        </select>
                      </label>
                    )}
                  </div>
                </div>
              ))}
            </div>
          </section>

          {!decision ? (
            <button
              type="button"
              className="full-action"
              disabled={busy || receipt.findings.length === 0}
              onClick={() => void finalizeDecisions()}
            >
              {busy ? <LoaderCircle size={15} className="spin" /> : <ShieldCheck size={15} />}
              确认决策并生成 follow-up
            </button>
          ) : (
            <div className="reader-workbench-decision">
              <strong>决策收据已生成</strong>
              <span>
                adopted={decision.adopted_finding_ids.length}
                · rejected={decision.rejected_finding_ids.length}
                · deferred={decision.deferred_finding_ids.length}
              </span>
              <span>parent_reader_valid_for_new_candidate=false</span>
              {onOpenChanges && (
                <button type="button" className="quiet-button" onClick={onOpenChanges}>
                  在改动页继续反馈晋升
                </button>
              )}
            </div>
          )}
        </>
      )}

      {active && !receipt && !error && !busy && (
        <div className="distillation-empty">
          <BookOpenText size={16} />
          <span>尚未对当前结果运行冷读。点击右上角刷新图标开始。</span>
        </div>
      )}
    </section>
  )
}

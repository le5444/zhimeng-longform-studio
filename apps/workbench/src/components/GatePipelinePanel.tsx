import {
  AlertTriangle,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  evaluateGatePipeline,
  getGatePipeline,
  type GatePipelineReceipt,
} from '../api/gatePipeline'
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

const STEP_LABELS: Record<string, string> = {
  post_draft_gate: 'Post-draft',
  literary_quality_gate: '文学质量',
  llm_literary_score: 'LLM 形分',
  external_blind_review_matrix: '外部盲评',
  hard_fact_gate: '硬事实',
}

function stepTone(status: string, required: boolean): 'pass' | 'warn' | 'fail' {
  const normalized = status.toLowerCase()
  if (['passed', 'complete', 'ready', 'advisory_only', 'offline_stub'].includes(normalized)) {
    return required ? 'pass' : 'warn'
  }
  if (['missing', 'missing_evidence', 'missing_inputs', 'incomplete', 'stale'].includes(normalized)) {
    return 'warn'
  }
  return 'fail'
}

export function GatePipelinePanel({
  enabled,
  changeSetId,
  reviewRevision = null,
}: {
  enabled: boolean
  changeSetId: string | null
  reviewRevision?: number | null
}) {
  const [receipt, setReceipt] = useState<GatePipelineReceipt | null>(null)
  const [notClaiming, setNotClaiming] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!enabled || !changeSetId) {
      setReceipt(null)
      setNotClaiming([])
      return
    }
    setBusy(true)
    setError(null)
    try {
      const current = await getGatePipeline(changeSetId)
      setReceipt(current.receipt)
      setNotClaiming([...current.not_claiming])
    } catch (currentError) {
      setReceipt(null)
      setNotClaiming([])
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [changeSetId, enabled])

  const evaluate = useCallback(async () => {
    if (!enabled || !changeSetId) return
    setBusy(true)
    setError(null)
    try {
      const response = await evaluateGatePipeline(changeSetId, {
        expected_review_revision: reviewRevision ?? undefined,
      })
      setReceipt(response.receipt)
      setNotClaiming([...response.not_claiming])
    } catch (currentError) {
      setReceipt(null)
      setNotClaiming([])
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [changeSetId, enabled, reviewRevision])

  useEffect(() => {
    void load()
  }, [load])

  const healthy = receipt?.status === 'complete'
  const stepCount = useMemo(() => receipt?.steps.length ?? 0, [receipt])

  return (
    <div className="inspector-body gate-panel gate-pipeline-panel" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className={`distillation-health-icon ${healthy ? 'health-ok' : 'health-blocked'}`}>
          {busy
            ? <LoaderCircle size={15} className="spin" />
            : healthy
              ? <ShieldCheck size={15} />
              : <AlertTriangle size={15} />}
        </span>
        <div>
          <strong>Gate Pipeline</strong>
          <span>
            {receipt
              ? `status=${receipt.status} · steps=${stepCount} · auth_ready=${String(receipt.authorization_input_ready)} · quality_assessed=false`
              : enabled
                ? (changeSetId ? '尚未运行有序 Gate 链' : '需要 ready/applied ChangeSet')
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="评估 Gate Pipeline"
          disabled={!enabled || !changeSetId || busy}
          onClick={() => void evaluate()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      {!enabled && <div className="distillation-empty"><AlertTriangle size={16} /><span>Writing Runtime 未就绪</span></div>}
      {enabled && !changeSetId && <div className="distillation-empty"><AlertTriangle size={16} /><span>请选择 ready / applied / rolled_back 的 ChangeSet</span></div>}
      {error && <div className="agent-inline-error" role="alert"><AlertTriangle size={14} /><span>{error}</span></div>}

      {receipt && (
        <>
          <div className="gate-summary literary-quality-summary">
            {healthy ? <ShieldCheck size={19} /> : <AlertTriangle size={19} />}
            <div>
              <strong>
                {healthy
                  ? '授权输入可就绪 · 仍不解锁正式正文'
                  : '流水线未完成或被阻断 · fail-closed'}
              </strong>
              <span>
                policy {receipt.policy_version} · post_draft={receipt.post_draft_gate_status}
                {' · '}matrix_ready={String(receipt.external_blind_review_matrix_ready ?? false)}
              </span>
            </div>
            <div className="literary-total-score" aria-label="pipeline status">
              <strong>{receipt.status}</strong>
              <span>STATUS</span>
            </div>
          </div>

          <div className="llm-score-flags" aria-label="gate pipeline honesty flags">
            <span>authorization_input_ready={String(receipt.authorization_input_ready)}</span>
            <span>formal_draft_allowed=false</span>
            <span>quality_assessed=false</span>
            <span>authorization_unlock=false</span>
          </div>

          <section>
            <span className="section-label">有序步骤</span>
            <ol className="gate-pipeline-steps-grid">
              {receipt.steps.map((step) => {
                const tone = stepTone(step.status, step.required)
                return (
                  <li className={`gate-pipeline-step-card tone-${tone}`} key={`${step.ordinal}-${step.code}`}>
                    <div className="gate-pipeline-step-head">
                      <strong>{step.ordinal}. {STEP_LABELS[step.code] ?? step.code}</strong>
                      <span>{step.required ? 'required' : 'advisory'}</span>
                    </div>
                    <em>{step.status} · {step.present ? 'present' : 'missing'}</em>
                    <small>{step.detail}</small>
                  </li>
                )
              })}
            </ol>
          </section>

          {notClaiming.length > 0 && (
            <section>
              <span className="section-label">not_claiming</span>
              <ul className="reader-questions">
                {notClaiming.map((item) => (
                  <li key={item}><code>{item}</code></li>
                ))}
              </ul>
            </section>
          )}
        </>
      )}
    </div>
  )
}

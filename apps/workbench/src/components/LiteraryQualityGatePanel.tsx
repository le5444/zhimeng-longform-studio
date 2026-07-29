import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  evaluateLiteraryQualityGate,
  getLiteraryQualityGate,
  type LiteraryQualityGateReceipt,
} from '../api/literaryQualityGate'
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

function rubricTone(score: number): 'pass' | 'warn' | 'fail' {
  if (score >= 75) return 'pass'
  if (score >= 55) return 'warn'
  return 'fail'
}

const RUBRIC_LABELS: Record<string, string> = {
  dialogue_balance: '对白平衡',
  sensory_density: '感官密度',
  pacing_variation: '节奏变化',
  hook_opening: '开场钩子',
  repetition_risk: '重复风险',
}

export function LiteraryQualityGatePanel({
  enabled,
  changeSetId,
  reviewRevision = null,
}: {
  enabled: boolean
  changeSetId: string | null
  reviewRevision?: number | null
}) {
  const [receipt, setReceipt] = useState<LiteraryQualityGateReceipt | null>(null)
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
      const current = await getLiteraryQualityGate(changeSetId)
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
      const response = await evaluateLiteraryQualityGate(changeSetId, {
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

  return (
    <div className="inspector-body gate-panel literary-quality-panel" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className="distillation-health-icon health-blocked">
          {busy ? <LoaderCircle size={15} className="spin" /> : <ShieldAlert size={15} />}
        </span>
        <div>
          <strong>文学质量 Gate</strong>
          <span>
            {receipt
              ? `decision=${receipt.decision} · rubric=${receipt.rubric_total_score ?? '—'} · quality_assessed=false`
              : enabled
                ? (changeSetId ? '尚未评估当前 ChangeSet' : '需要 ready/applied ChangeSet')
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="评估文学质量 Gate"
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
            <ShieldAlert size={19} />
            <div>
              <strong>仅建议，不解锁正式正文</strong>
              <span>
                policy {receipt.policy_version} · {receipt.outcome} · mode={receipt.gate_mode} · receipt{' '}
                {receipt.receipt_id.slice(0, 22)}
              </span>
            </div>
            <div className="literary-total-score" aria-label="rubric 总分">
              <strong>{receipt.rubric_total_score ?? '—'}</strong>
              <span>TOTAL</span>
            </div>
          </div>

          {receipt.rubric_scores && receipt.rubric_scores.length > 0 && (
            <section>
              <span className="section-label">确定性文学 rubric</span>
              <div className="literary-rubric-grid">
                {receipt.rubric_scores.map((item) => {
                  const tone = rubricTone(item.score)
                  return (
                    <div className={`literary-rubric-card tone-${tone}`} key={item.code}>
                      <div className="literary-rubric-head">
                        <strong>{RUBRIC_LABELS[item.code] ?? item.code}</strong>
                        <span>{item.score}</span>
                      </div>
                      <div className="score-bar" aria-hidden="true">
                        <i style={{ width: `${Math.max(0, Math.min(100, item.score))}%` }} />
                      </div>
                      <small>w={item.weight} · {item.detail}</small>
                    </div>
                  )
                })}
              </div>
              <div className="gate-list">
                <div className="gate-row gate-warn">
                  <span className="gate-level">TOTAL</span>
                  <span className="gate-icon"><ShieldAlert size={15} /></span>
                  <div>
                    <strong>rubric_total_score={receipt.rubric_total_score}</strong>
                    <span>deterministic only · not quality_assessed · not LLM score</span>
                  </div>
                </div>
              </div>
            </section>
          )}

          <section>
            <span className="section-label">维度</span>
            <div className="gate-list">
              {receipt.dimensions.map((item) => (
                <div className={`gate-row gate-${item.status === 'pass' ? 'pass' : item.status === 'watch' ? 'warn' : 'fail'}`} key={item.code}>
                  <span className="gate-level">{item.status.toUpperCase()}</span>
                  <span className="gate-icon">{item.status === 'pass' ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span>
                  <div><strong>{item.code}</strong><span>{item.detail}</span></div>
                </div>
              ))}
            </div>
          </section>

          {receipt.findings.length > 0 && (
            <section>
              <span className="section-label">结构发现</span>
              <div className="gate-list">
                {receipt.findings.slice(0, 12).map((item) => (
                  <div className="gate-row gate-fail" key={item.finding_id}>
                    <span className="gate-level">{item.severity.toUpperCase()}</span>
                    <span className="gate-icon"><AlertTriangle size={15} /></span>
                    <div>
                      <strong>{item.kind}</strong>
                      <span>{item.summary}{item.relative_path ? ` · ${item.relative_path}` : ''}</span>
                    </div>
                  </div>
                ))}
              </div>
            </section>
          )}

          <section>
            <span className="section-label">not_claiming</span>
            <ul className="reader-questions">
              {notClaiming.map((item) => <li key={item}><code>{item}</code></li>)}
            </ul>
          </section>
        </>
      )}
    </div>
  )
}

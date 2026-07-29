import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  Sparkles,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  evaluateLlmLiteraryScore,
  getLlmLiteraryScore,
  type LlmLiteraryScoreReceipt,
} from '../api/llmLiteraryScore'
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

function scoreTone(score: number): 'pass' | 'warn' | 'fail' {
  if (score >= 75) return 'pass'
  if (score >= 55) return 'warn'
  return 'fail'
}

const AXIS_LABELS: Record<string, string> = {
  immersion: '沉浸感',
  character_voice: '角色声口',
  emotional_arc: '情绪弧',
  prose_craft: '文笔',
  suspense_pull: '悬念拉扯',
}

export function LlmLiteraryScorePanel({
  enabled,
  changeSetId,
  reviewRevision = null,
}: {
  enabled: boolean
  changeSetId: string | null
  reviewRevision?: number | null
}) {
  const [receipt, setReceipt] = useState<LlmLiteraryScoreReceipt | null>(null)
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
      const current = await getLlmLiteraryScore(changeSetId)
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
      const response = await evaluateLlmLiteraryScore(changeSetId, {
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
    <div className="inspector-body gate-panel llm-literary-score-panel" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className="distillation-health-icon health-blocked">
          {busy ? <LoaderCircle size={15} className="spin" /> : <Sparkles size={15} />}
        </span>
        <div>
          <strong>LLM 形文学分</strong>
          <span>
            {receipt
              ? `mode=${receipt.scorer_mode} · total=${receipt.total_score} · quality_assessed=false`
              : enabled
                ? (changeSetId ? '尚未评估当前 ChangeSet' : '需要 ready/applied ChangeSet')
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="评估 LLM 形文学分"
          disabled={!enabled || !changeSetId || busy}
          onClick={() => void evaluate()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      {!enabled && (
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>Writing Runtime 未就绪</span>
        </div>
      )}
      {enabled && !changeSetId && (
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>请选择 ready / applied / rolled_back 的 ChangeSet</span>
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
          <div className="gate-summary literary-quality-summary llm-score-summary">
            <Sparkles size={19} />
            <div>
              <strong>仅建议 · offline stub · 不解锁正式正文</strong>
              <span>
                policy {receipt.policy_version} · {receipt.outcome} · mode={receipt.scorer_mode} · receipt{' '}
                {receipt.receipt_id.slice(0, 22)}
              </span>
            </div>
            <div className="literary-total-score" aria-label="LLM 形总分">
              <strong>{receipt.total_score}</strong>
              <span>TOTAL</span>
            </div>
          </div>

          <div className="llm-score-flags" aria-label="LLM score honesty flags">
            <span>advisory_only={String(receipt.advisory_only)}</span>
            <span>llm_score_recorded={String(receipt.llm_score_recorded)}</span>
            <span>quality_assessed=false</span>
            <span>formal_draft_allowed=false</span>
          </div>

          <section>
            <span className="section-label">五轴 LLM 形评分</span>
            <div className="literary-rubric-grid">
              {receipt.axes.map((item) => {
                const tone = scoreTone(item.score)
                return (
                  <div className={`literary-rubric-card tone-${tone}`} key={item.code}>
                    <div className="literary-rubric-head">
                      <strong>{AXIS_LABELS[item.code] ?? item.code}</strong>
                      <span>{item.score}</span>
                    </div>
                    <div className="score-bar" aria-hidden="true">
                      <i style={{ width: `${Math.max(0, Math.min(100, item.score))}%` }} />
                    </div>
                    <small>w={item.weight} · {item.rationale}</small>
                  </div>
                )
              })}
            </div>
          </section>

          <section>
            <span className="section-label">详情</span>
            <div className="gate-list">
              <div className="gate-row gate-warn">
                <span className="gate-level">ADVISORY</span>
                <span className="gate-icon">
                  <CheckCircle2 size={15} />
                </span>
                <div>
                  <strong>{receipt.detail}</strong>
                  <span>provider_model={receipt.provider_model ?? 'null'} · not live quality unlock</span>
                </div>
              </div>
            </div>
          </section>

          <section>
            <span className="section-label">not_claiming</span>
            <ul className="reader-questions">
              {notClaiming.map((item) => (
                <li key={item}>
                  <code>{item}</code>
                </li>
              ))}
            </ul>
          </section>
        </>
      )}
    </div>
  )
}

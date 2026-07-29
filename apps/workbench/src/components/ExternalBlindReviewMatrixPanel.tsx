import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  Users,
} from 'lucide-react'
import { useCallback, useState } from 'react'
import {
  evaluateExternalBlindReviewMatrix,
  type ExternalBlindReviewMatrixReceipt,
} from '../api/externalBlindReviewMatrix'
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

export function ExternalBlindReviewMatrixPanel({
  enabled,
}: {
  enabled: boolean
}) {
  const [receipt, setReceipt] = useState<ExternalBlindReviewMatrixReceipt | null>(null)
  const [notClaiming, setNotClaiming] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const evaluate = useCallback(async () => {
    if (!enabled) return
    setBusy(true)
    setError(null)
    try {
      // Fail-closed empty evaluation by default; real evidence is supplied via API/tools.
      const response = await evaluateExternalBlindReviewMatrix({})
      setReceipt(response.receipt)
      setNotClaiming([...response.not_claiming])
    } catch (currentError) {
      setReceipt(null)
      setNotClaiming([])
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [enabled])

  return (
    <div className="inspector-body gate-panel external-blind-matrix-panel" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className="distillation-health-icon health-blocked">
          {busy ? <LoaderCircle size={15} className="spin" /> : <Users size={15} />}
        </span>
        <div>
          <strong>外部盲评矩阵</strong>
          <span>
            {receipt
              ? `ready=${String(receipt.matrix_ready)} · passed=${receipt.passed_cell_count}/${receipt.required_cell_count} · quality_assessed=false`
              : enabled
                ? '尚未评估；缺证据时 fail-closed'
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="评估外部盲评矩阵"
          disabled={!enabled || busy}
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
      {error && (
        <div className="agent-inline-error" role="alert">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      )}

      {receipt && (
        <>
          <div className="gate-summary literary-quality-summary">
            {receipt.matrix_ready ? <CheckCircle2 size={19} /> : <AlertTriangle size={19} />}
            <div>
              <strong>
                {receipt.matrix_ready
                  ? '矩阵完整 · 仍不宣称 quality_assessed'
                  : '矩阵未就绪 · fail-closed'}
              </strong>
              <span>
                policy {receipt.policy_version} · suite={receipt.suite_id ?? '—'} · receipt{' '}
                {receipt.receipt_id.slice(0, 24)}
              </span>
            </div>
            <div className="literary-total-score" aria-label="盲评通过格">
              <strong>
                {receipt.passed_cell_count}/{receipt.required_cell_count}
              </strong>
              <span>CELLS</span>
            </div>
          </div>

          <div className="llm-score-flags" aria-label="external blind honesty flags">
            <span>matrix_ready={String(receipt.matrix_ready)}</span>
            <span>quality_assessed=false</span>
            <span>formal_draft_allowed=false</span>
            <span>local_v1_exe=false</span>
          </div>

          <section>
            <span className="section-label">6 格矩阵</span>
            <div className="external-blind-grid">
              {receipt.cells.map((cell) => (
                <div
                  key={`${cell.reviewer_id}:${cell.sample_id}`}
                  className={`external-blind-cell status-${cell.status}`}
                >
                  <strong>
                    {cell.reviewer_id.replace('external_reviewer_', 'R')}
                    {' · '}
                    {cell.sample_id.replace('chapter_sample_', 'S')}
                  </strong>
                  <span>{cell.status}</span>
                  <small>{cell.detail}</small>
                </div>
              ))}
            </div>
          </section>

          <section>
            <span className="section-label">详情</span>
            <div className="gate-list">
              <div className={`gate-row ${receipt.matrix_ready ? 'gate-pass' : 'gate-fail'}`}>
                <span className="gate-level">{receipt.matrix_ready ? 'READY' : 'OPEN'}</span>
                <span className="gate-icon">
                  {receipt.matrix_ready ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
                </span>
                <div>
                  <strong>{receipt.detail}</strong>
                  <span>
                    missing={receipt.missing_required.length}
                    {receipt.missing_required.length > 0
                      ? ` · ${receipt.missing_required.slice(0, 3).join(', ')}`
                      : ''}
                  </span>
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

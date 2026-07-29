import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  evaluateHardFactGate,
  getHardFactGate,
  type HardFactGateReceipt,
} from '../api/hardFactGate'
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

export function HardFactGatePanel({
  enabled,
  changeSetId,
  reviewRevision = null,
}: {
  enabled: boolean
  changeSetId: string | null
  reviewRevision?: number | null
}) {
  const [receipt, setReceipt] = useState<HardFactGateReceipt | null>(null)
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
      const current = await getHardFactGate(changeSetId)
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
      const response = await evaluateHardFactGate(changeSetId, {
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
    <div className="inspector-body gate-panel hard-fact-panel" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className={`distillation-health-icon ${receipt?.status === 'passed' ? 'health-ok' : 'health-blocked'}`}>
          {busy ? <LoaderCircle size={15} className="spin" /> : receipt?.status === 'passed' ? <ShieldCheck size={15} /> : <AlertTriangle size={15} />}
        </span>
        <div>
          <strong>硬事实 Gate</strong>
          <span>
            {receipt
              ? `status=${receipt.status} · authorization_input_ready=${String(receipt.authorization_input_ready)}`
              : enabled
                ? (changeSetId ? '尚未评估当前 ChangeSet' : '需要 ready/applied ChangeSet')
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="评估硬事实 Gate"
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
          <div className="gate-summary">
            {receipt.status === 'passed' ? <CheckCircle2 size={19} /> : <AlertTriangle size={19} />}
            <div>
              <strong>
                {receipt.status === 'passed'
                  ? '结构硬事实输入已就绪（仍不解锁正式正文）'
                  : '结构硬事实未通过授权输入'}
              </strong>
              <span>policy {receipt.policy_version} · post_draft={receipt.post_draft_gate_status} · {receipt.outcome}</span>
            </div>
          </div>
          {receipt.signals.length > 0 && (
            <section>
              <span className="section-label">硬事实信号</span>
              <div className="gate-list">
                {receipt.signals.slice(0, 12).map((item, index) => (
                  <div className={`gate-row gate-${item.severity === 'block' ? 'fail' : 'warn'}`} key={`${item.code}-${index}`}>
                    <span className="gate-level">{item.severity.toUpperCase()}</span>
                    <span className="gate-icon"><AlertTriangle size={15} /></span>
                    <div>
                      <strong>{item.code}</strong>
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

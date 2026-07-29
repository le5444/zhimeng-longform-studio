import {
  AlertTriangle,
  CheckCircle2,
  LoaderCircle,
  LockKeyhole,
  RefreshCw,
  ShieldX,
} from 'lucide-react'
import { useCallback, useEffect, useState } from 'react'
import {
  evaluateFormalDraftAuthorization,
  evaluateFormalDraftContinuousO2,
  evaluateFormalDraftProvenance,
  getFormalDraftAuthorDecision,
  recordFormalDraftAuthorDecision,
  type FormalDraftAuthorDecisionReceipt,
  type FormalDraftAuthorIntent,
  type FormalDraftAuthorizationReceipt,
  type FormalDraftContinuousO2Receipt,
  type FormalDraftProvenanceReceipt,
} from '../api/formalDraftAuthorization'
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

export function FormalDraftAuthorizationPanel({
  enabled,
  projectId,
  projectRevision = null,
  projectFingerprint = null,
  changeSetId = null,
}: {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  changeSetId?: string | null
}) {
  const [receipt, setReceipt] = useState<FormalDraftAuthorizationReceipt | null>(null)
  const [authorDecision, setAuthorDecision] = useState<FormalDraftAuthorDecisionReceipt | null>(null)
  const [provenance, setProvenance] = useState<FormalDraftProvenanceReceipt | null>(null)
  const [continuousO2, setContinuousO2] = useState<FormalDraftContinuousO2Receipt | null>(null)
  const [notClaiming, setNotClaiming] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!enabled || !projectId) return
    setBusy(true)
    setError(null)
    try {
      const [authResponse, decisionResponse, provenanceResponse, continuousResponse] = await Promise.all([
        evaluateFormalDraftAuthorization(projectId, {
          expected_project_revision: projectRevision ?? undefined,
          expected_project_fingerprint: projectFingerprint ?? undefined,
          change_set_id: changeSetId ?? undefined,
        }),
        getFormalDraftAuthorDecision(projectId),
        evaluateFormalDraftProvenance(projectId, {
          expected_project_revision: projectRevision ?? undefined,
          expected_project_fingerprint: projectFingerprint ?? undefined,
          change_set_id: changeSetId ?? undefined,
        }),
        evaluateFormalDraftContinuousO2(projectId, {
          expected_project_revision: projectRevision ?? undefined,
          expected_project_fingerprint: projectFingerprint ?? undefined,
        }),
      ])
      setReceipt(authResponse.receipt)
      setNotClaiming([...authResponse.not_claiming])
      setAuthorDecision(decisionResponse.receipt)
      setProvenance(provenanceResponse.receipt)
      setContinuousO2(continuousResponse.receipt)
    } catch (currentError) {
      setReceipt(null)
      setAuthorDecision(null)
      setProvenance(null)
      setContinuousO2(null)
      setNotClaiming([])
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [changeSetId, enabled, projectFingerprint, projectId, projectRevision])

  const decide = useCallback(async (intent: FormalDraftAuthorIntent) => {
    if (!enabled || !projectId) return
    setBusy(true)
    setError(null)
    try {
      const decisionResponse = await recordFormalDraftAuthorDecision(projectId, {
        intent,
        expected_project_revision: projectRevision ?? undefined,
        expected_project_fingerprint: projectFingerprint ?? undefined,
      })
      setAuthorDecision(decisionResponse.receipt)
      const authResponse = await evaluateFormalDraftAuthorization(projectId, {
        expected_project_revision: projectRevision ?? undefined,
        expected_project_fingerprint: projectFingerprint ?? undefined,
      })
      setReceipt(authResponse.receipt)
      setNotClaiming([...authResponse.not_claiming])
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusy(false)
    }
  }, [changeSetId, enabled, projectFingerprint, projectId, projectRevision])

  useEffect(() => {
    void refresh()
  }, [refresh])

  return (
    <div className="inspector-body gate-panel formal-draft-panel" role="tabpanel" id="inspector-panel-gate" aria-labelledby="inspector-tab-gate" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className="distillation-health-icon health-blocked">
          {busy ? <LoaderCircle size={15} className="spin" /> : <LockKeyhole size={15} />}
        </span>
        <div>
          <strong>正式起稿授权</strong>
          <span>
            {receipt
              ? `decision=${receipt.decision} · formal_draft_allowed=${String(receipt.formal_draft_allowed)}`
              : enabled
                ? '评估 Local v1 正式正文写权'
                : 'Runtime 未就绪'}
          </span>
        </div>
        <button type="button" className="icon-button" aria-label="重新评估正式起稿授权" disabled={!enabled || !projectId || busy} onClick={() => void refresh()}>
          <RefreshCw size={14} />
        </button>
      </header>

      {!enabled && <div className="distillation-empty"><AlertTriangle size={16} /><span>Writing Runtime 未就绪</span></div>}
      {enabled && !projectId && <div className="distillation-empty"><AlertTriangle size={16} /><span>请先绑定小说项目</span></div>}
      {error && <div className="agent-inline-error" role="alert"><AlertTriangle size={14} /><span>{error}</span></div>}

      {enabled && projectId && (
        <section>
          <span className="section-label">作者决策</span>
          <div className="gate-summary">
            <LockKeyhole size={19} />
            <div>
              <strong>
                {authorDecision
                  ? `intent=${authorDecision.intent}`
                  : '尚未记录作者正式起稿授权决策'}
              </strong>
              <span>
                {authorDecision
                  ? `effective=${String(authorDecision.authorization_effective)} · 不授予 formal_draft`
                  : '可请求授权评估，但 Local v1 仍锁定写权'}
              </span>
            </div>
          </div>
          <div className="composer-actions" style={{ gap: 8, marginTop: 8 }}>
            <button type="button" className="quiet-button" disabled={busy} onClick={() => void decide('request_authorization')}>
              请求正式起稿授权
            </button>
            <button type="button" className="quiet-button" disabled={busy} onClick={() => void decide('withhold_authorization')}>
              暂不授权
            </button>
            <button type="button" className="quiet-button" disabled={busy || !authorDecision} onClick={() => void decide('revoke_authorization')}>
              撤销请求
            </button>
          </div>
        </section>
      )}

      {provenance && (
        <section>
          <span className="section-label">正式稿溯源</span>
          <div className="gate-summary">
            {provenance.status === 'complete' ? <CheckCircle2 size={19} /> : <AlertTriangle size={19} />}
            <div>
              <strong>status={provenance.status} · input_ready={String(provenance.authorization_input_ready)}</strong>
              <span>不解锁 formal_draft · missing={(provenance.missing_required ?? []).join(',') || 'none'}</span>
            </div>
          </div>
          <div className="gate-list">
            {provenance.checks.map((item) => (
              <div className={`gate-row gate-${item.present || !item.required ? 'pass' : 'fail'}`} key={item.code}>
                <span className="gate-level">{item.required ? (item.present ? 'OK' : 'MISS') : 'OPT'}</span>
                <span className="gate-icon">{item.present || !item.required ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span>
                <div><strong>{item.code}</strong><span>{item.detail}</span></div>
              </div>
            ))}
          </div>
        </section>
      )}

      {continuousO2 && (
        <section>
          <span className="section-label">连续 O2 就绪</span>
          <div className="gate-summary">
            {continuousO2.authorization_input_ready ? <CheckCircle2 size={19} /> : <AlertTriangle size={19} />}
            <div>
              <strong>status={continuousO2.status} · input_ready={String(continuousO2.authorization_input_ready)}</strong>
              <span>不宣称 exactly-once · missing={(continuousO2.missing_required ?? []).join(',') || 'none'}</span>
            </div>
          </div>
          <div className="gate-list">
            {(continuousO2.checks ?? []).map((item) => (
              <div className={`gate-row gate-${item.present || !item.required ? 'pass' : 'fail'}`} key={item.code}>
                <span className="gate-level">{item.required ? (item.present ? 'OK' : 'MISS') : 'OPT'}</span>
                <span className="gate-icon">{item.present || !item.required ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span>
                <div><strong>{item.code}</strong><span>{item.detail}</span></div>
              </div>
            ))}
          </div>
        </section>
      )}

      {receipt && (
        <>
          <div className="gate-summary">
            <ShieldX size={19} />
            <div>
              <strong>正式正文写权未授予</strong>
              <span>policy {receipt.policy_version} · receipt {receipt.receipt_id.slice(0, 22)}</span>
            </div>
          </div>
          <section>
            <span className="section-label">拒绝原因</span>
            <div className="gate-list">
              {receipt.denials.map((item) => (
                <div className="gate-row gate-fail" key={item.code}>
                  <span className="gate-level">DENY</span>
                  <span className="gate-icon"><AlertTriangle size={15} /></span>
                  <div><strong>{item.code}</strong><span>{item.message}</span></div>
                </div>
              ))}
            </div>
          </section>
          <section>
            <span className="section-label">前置条件</span>
            <div className="gate-list">
              {receipt.prerequisites.map((item) => (
                <div className={`gate-row gate-${item.satisfied ? 'pass' : 'fail'}`} key={item.code}>
                  <span className="gate-level">{item.satisfied ? 'OK' : 'TODO'}</span>
                  <span className="gate-icon">{item.satisfied ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}</span>
                  <div><strong>{item.code}</strong><span>{item.detail}</span></div>
                </div>
              ))}
            </div>
          </section>
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

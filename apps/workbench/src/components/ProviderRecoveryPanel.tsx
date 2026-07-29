import {
  AlertTriangle,
  CheckCircle2,
  Database,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  XCircle,
} from 'lucide-react'
import type { ProviderRecoveryAction } from '../api/agent'
import type { ProviderRecoverySnapshot } from '../hooks/useProviderRecovery'

interface ProviderRecoveryPanelProps {
  snapshot: ProviderRecoverySnapshot
  compact?: boolean
  onDecisionCommitted?: () => void
  onRecompile?: () => void
  recompileDisabled?: boolean
}

function candidateStateLabel(state: 'persisted' | 'absent' | 'invalid'): string {
  if (state === 'persisted') return '已持久化'
  if (state === 'invalid') return '校验失败'
  return '没有候选'
}

function decisionLabel(action: ProviderRecoveryAction): string {
  return action === 'adopt_persisted_candidate' ? '已采纳持久化候选' : '已放弃未知结果'
}

export function ProviderRecoveryPanel({
  snapshot,
  compact = false,
  onDecisionCommitted,
  onRecompile,
  recompileDisabled = false,
}: ProviderRecoveryPanelProps) {
  if (snapshot.state === 'idle') return null
  if (snapshot.state === 'loading') {
    return <div className="pending-run provider-recovery-state" role="status"><LoaderCircle size={15} className="spin" /><div><strong>正在读取 Provider 恢复记录</strong><span>正在校验 Dossier、候选身份和 CAS 版本</span></div></div>
  }
  if (snapshot.state === 'missing') {
    return <div className="pending-run provider-recovery-state" role="status"><ShieldAlert size={15} /><div><strong>没有 Provider 恢复记录</strong><span>旧请求不会自动重放；可重新检查 Runtime 是否已持久化 Dossier。</span><button type="button" className="quiet-button" onClick={snapshot.refresh}><RefreshCw size={13} />重新检查</button></div></div>
  }
  if (snapshot.state === 'error' || !snapshot.response) {
    return <div className="pending-run provider-recovery-state provider-recovery-error" role="alert"><AlertTriangle size={15} /><div><strong>Provider 恢复记录读取失败</strong><span>{snapshot.error ?? 'Runtime 返回了无效的恢复记录'}</span><button type="button" className="quiet-button" onClick={snapshot.refresh}><RefreshCw size={13} />重试</button></div></div>
  }

  const { dossier, decision } = snapshot.response
  const pending = decision === null
  const busy = snapshot.action !== null
  const submit = (nextAction: ProviderRecoveryAction) => {
    void snapshot.decide(nextAction).then((committed) => {
      if (committed) onDecisionCommitted?.()
    })
  }

  return (
    <section className={`provider-recovery-panel${compact ? ' is-compact' : ''}`} aria-label="Provider 恢复">
      <header className="provider-recovery-header">
        <ShieldAlert size={17} />
        <div>
          <strong>{pending ? 'Provider 结果待人工决策' : decisionLabel(decision.action)}</strong>
          <span>远端结果不可判定，旧 Provider 请求不会自动重放。</span>
        </div>
        <span className={`provider-recovery-badge${pending ? '' : ' is-decided'}`}>{pending ? '待决策' : '已决策'}</span>
      </header>

      <div className="provider-recovery-metrics">
        <div><span>远端结果</span><strong>未知</strong><code>{dossier.remote_result}</code></div>
        <div><span>候选状态</span><strong>{candidateStateLabel(dossier.candidate_state)}</strong><code>{dossier.candidate_state}</code></div>
        <div><span>Provider 重放</span><strong>{dossier.provider_replay_count} 次</strong><code>provider_replay_count={dossier.provider_replay_count}</code></div>
        <div><span>Dossier 版本</span><strong>revision {dossier.dossier_revision}</strong><code>{dossier.dossier_id}</code></div>
      </div>

      {decision && <div className={`provider-recovery-decision decision-${decision.action}`} role="status">{decision.action === 'adopt_persisted_candidate' ? <CheckCircle2 size={15} /> : <XCircle size={15} />}<div><strong>{decisionLabel(decision.action)}</strong><span>Run 结果：{decision.resulting_run_status} · Dossier revision {decision.previous_dossier_revision} to {decision.new_dossier_revision}</span>{decision.action === 'abandon_and_recompile' && onRecompile && <button type="button" className="quiet-button" disabled={recompileDisabled} onClick={onRecompile}><RefreshCw size={13} />重新编译新 Run</button>}</div></div>}

      {pending && <div className="provider-recovery-actions" aria-label="Provider 恢复操作">
        <button type="button" className="recovery-adopt" disabled={!snapshot.canAdopt || busy} title={snapshot.canAdopt ? '采纳已校验并持久化的候选输出' : '需先读取并校验匹配的持久化候选'} onClick={() => submit('adopt_persisted_candidate')}>{snapshot.action === 'adopt_persisted_candidate' ? <LoaderCircle size={14} className="spin" /> : <Database size={14} />}<span>采纳已保存候选</span><code>adopt_persisted_candidate</code></button>
        <button type="button" className="recovery-abandon" disabled={busy} title="放弃未知远端结果；后续由作者重新编译新 Run" onClick={() => submit('abandon_and_recompile')}>{snapshot.action === 'abandon_and_recompile' ? <LoaderCircle size={14} className="spin" /> : <RotateCcw size={14} />}<span>放弃未知结果</span><code>abandon_and_recompile</code></button>
      </div>}

      {snapshot.actionError && <div className="provider-recovery-action-error" role="alert"><AlertTriangle size={13} /><span>{snapshot.actionError}</span></div>}

      {!compact && <details className="provider-recovery-provenance">
        <summary>恢复来源与完整性</summary>
        <div>
          <span>Run</span><code>{dossier.run_id}</code>
          <span>Provider boundary</span><code>{dossier.provider_boundary_id}</code>
          <span>Candidate output</span><code>{dossier.candidate_output_id ?? 'null'}</code>
          <span>Candidate hash</span><code>{dossier.candidate_output_hash ?? 'null'}</code>
          <span>Provider request hash</span><code>{dossier.provider_request_hash}</code>
          <span>Dossier hash</span><code>{dossier.dossier_hash}</code>
        </div>
      </details>}
    </section>
  )
}

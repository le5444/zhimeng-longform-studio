import { useEffect, useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronUp, CircleAlert, CircleStop, RotateCw, Settings2, X } from 'lucide-react'
import type { AgentRun } from '../../api/agent'
import {
  readProviderCatalog,
  testProviderModelsLiveness,
  type ProviderLivenessResult,
} from '../../api/providerSettings'
import { authorFacingAgentRunError, authorFacingError, authorFacingErrorText } from '../../lib/authorFacingError'
import type { agentFileEffects } from '../lib/agentFileEffects'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'
import { T } from '../strings'
import { Btn, IconBtn, type Notify } from '../ui'
import { cleanModelName } from './models'
import { StepList } from './Steps'
import type { StepRow } from './stepRows'

interface ProbeGroup {
  providerId: string | null
  providerName: string
  active: boolean
  results: ProviderLivenessResult[]
  error: string | null
}

/**
 * 这一轮没跑完（出错 / 被停下 / 中断）：说清原因、停之前做了什么、文件动没动，给「接着做」「重发」
 * 「测一下哪些模型能用」（每条线路勾选的模型真试一句，能用的点「用这个重发」才换——代码从不自己换模型）。
 */
export function FailureNotice({ run, steps, replyKept, effects, canResume, resumeBusy, continuationReason, provider, onSelectModel, onOpenSettings, onResend, onResume, onShowCommands, notify }: {
  run: AgentRun
  steps: StepRow[]
  replyKept: boolean
  effects: ReturnType<typeof agentFileEffects>
  canResume: boolean
  resumeBusy: boolean
  continuationReason?: string
  provider: ProviderSettingsApi
  onSelectModel: (model: string, providerId: string | null) => Promise<void>
  onOpenSettings: () => void
  onResend: () => void
  onResume: () => void
  onShowCommands?: (id: string) => void
  notify: Notify
}) {
  const cancelled = run.status === 'cancelled'
  const failure = run.error?.error
  const reason = authorFacingAgentRunError(failure?.code, failure?.message, failure?.details)
  const storageKey = `zhimeng.dismissed-failure.${run.run_id}`
  const [dismissed, setDismissed] = useState(() => {
    try { return window.sessionStorage.getItem(storageKey) === '1' } catch { return false }
  })
  const [expanded, setExpanded] = useState(false)
  const [probeBusy, setProbeBusy] = useState(false)
  const [groups, setGroups] = useState<ProbeGroup[] | null>(null)
  const [pickBusy, setPickBusy] = useState<string | null>(null)
  const [picked, setPicked] = useState<string | null>(null)
  const probeAbort = useRef<AbortController | null>(null)
  useEffect(() => () => probeAbort.current?.abort(), [])

  const dismiss = (value: boolean) => {
    setDismissed(value)
    if (value) probeAbort.current?.abort()
    try {
      if (value) window.sessionStorage.setItem(storageKey, '1')
      else window.sessionStorage.removeItem(storageKey)
    } catch { /* The current view still remembers the choice. */ }
  }

  const probe = async () => {
    if (probeBusy) return
    const controller = new AbortController()
    probeAbort.current = controller
    setProbeBusy(true)
    try {
      let targets: Array<{ providerId: string | null; name: string; baseUrl: string; models: string[]; active: boolean }> = []
      try {
        const catalog = await readProviderCatalog({ signal: controller.signal })
        targets = [...catalog.providers]
          .sort((a, b) => Number(b.active) - Number(a.active))
          .map((profile) => ({
            providerId: profile.provider_id,
            name: profile.name,
            baseUrl: profile.base_url,
            models: [...new Set([profile.model, ...profile.selected_models].filter(Boolean))],
            active: profile.active,
          }))
          .filter((target) => target.baseUrl && target.models.length > 0)
      } catch {
        targets = []
      }
      const settings = provider.settings
      if (targets.length === 0 && settings?.configured && settings.base_url && settings.model) {
        targets = [{
          providerId: settings.provider_id ?? null,
          name: settings.provider_name ?? '',
          baseUrl: settings.base_url,
          models: [...new Set([settings.model, ...(settings.selected_models ?? [])].filter(Boolean))],
          active: true,
        }]
      }
      if (targets.length === 0) {
        notify('warn', T.agent.testModelsNone)
        return
      }
      const settled = await Promise.allSettled(targets.map((target) => testProviderModelsLiveness(
        { providerId: target.providerId ?? undefined, baseUrl: target.baseUrl, models: target.models },
        { signal: controller.signal },
      )))
      if (controller.signal.aborted) return
      setGroups(targets.map((target, index) => {
        const outcome = settled[index]
        return outcome.status === 'fulfilled'
          ? { providerId: target.providerId, providerName: target.name, active: target.active, results: outcome.value.results, error: null }
          : { providerId: target.providerId, providerName: target.name, active: target.active, results: [], error: authorFacingErrorText(outcome.reason, T.agent.testModelsFailed) }
      }))
    } finally {
      if (probeAbort.current === controller) probeAbort.current = null
      setProbeBusy(false)
    }
  }

  const pickAndResend = async (model: string, providerId: string | null) => {
    if (pickBusy) return
    setPickBusy(model)
    try {
      await onSelectModel(model, providerId)
      setPicked(model)
      notify('ok', T.agent.modelSwitched(cleanModelName(model)))
      onResend()
    } catch (error) {
      notify('danger', authorFacingError(error, T.agent.modelSwitchFailed))
    } finally {
      setPickBusy(null)
    }
  }

  const usable = useMemo(() => (groups ?? []).flatMap((group) => group.results.filter((item) => item.usable).map((item) => ({ group, item }))), [groups])
  const pickFastest = () => {
    const fastest = [...usable].sort((a, b) => (a.item.first_byte_ms ?? Infinity) - (b.item.first_byte_ms ?? Infinity))[0]
    if (fastest) void pickAndResend(fastest.item.model, fastest.group.providerId)
  }

  const effectsText = effects.writes.length > 0
    ? T.agent.savedBeforeStop(effects.writes.length)
    : effects.commandRan || effects.writeAttempted ? T.agent.checkFilesAfterStop : null

  if (dismissed) return <button type="button" className="zm-failure-restore" onClick={() => dismiss(false)}>查看上次{cancelled ? '停止' : '错误'}</button>

  return (
    <div className={`zm-failure${cancelled ? ' is-cancelled' : ''}`} role={cancelled ? 'status' : 'alert'}>
      <div className="zm-failure-head">
        {cancelled ? <CircleStop aria-hidden /> : <CircleAlert aria-hidden />}
        <div className="zm-failure-summary">
          <strong>{cancelled ? T.agent.runCancelled : T.agent.failedTitle}</strong>
          {!cancelled ? <p className={`zm-failure-reason${expanded ? '' : ' is-compact'}`}>{reason}</p> : null}
        </div>
        <div className="zm-failure-controls">
          {canResume ? <Btn size="sm" disabled={resumeBusy} onClick={onResume}>{T.agent.continueTask}</Btn> : null}
          <IconBtn label={T.agent.resend} icon={<RotateCw />} disabled={resumeBusy || pickBusy !== null} onClick={onResend} />
          <IconBtn label={expanded ? '收起错误详情' : '展开错误详情'} icon={expanded ? <ChevronUp /> : <ChevronDown />} aria-expanded={expanded} onClick={() => setExpanded(!expanded)} />
          <IconBtn label="关闭提示" icon={<X />} onClick={() => dismiss(true)} />
        </div>
      </div>
      {expanded ? <div className="zm-failure-detail">
      {failure?.message && failure.message !== reason ? <pre className="zm-failure-raw">{failure.message}</pre> : null}
      {effectsText ? <p className="zm-failure-note">{effectsText}</p> : null}
      {replyKept ? <p className="zm-failure-note">{T.agent.replyKept}</p> : null}
      {continuationReason ? <p className="zm-failure-note">{continuationReason}</p> : null}
      {steps.length > 0 ? (
        <details className="zm-failure-steps">
          <summary>{T.agent.didSteps(steps.length)}</summary>
          <StepList rows={steps} onShowCommands={onShowCommands} limit={20} />
        </details>
      ) : null}
      {groups ? (
        <div className="zm-probe">
          {groups.map((group, groupIndex) => (
            <div key={group.providerId ?? `current-${groupIndex}`} className="zm-probe-group">
              {groups.length > 1 ? <p className="zm-probe-name">{group.active ? `${group.providerName}（${T.settings.inUse}）` : group.providerName}</p> : null}
              {group.error ? <p className="zm-failure-note">{group.error}</p> : (
                <ul className="zm-probe-list">
                  {group.results.map((item) => (
                    <li key={item.model} className={item.usable ? 'is-ok' : 'is-bad'}>
                      <span className="zm-probe-model">{cleanModelName(item.model)}</span>
                      <span className="zm-probe-state">{item.usable ? T.settings.testResult.ok : (T.settings.testResult[item.status] ?? item.label)}</span>
                      {item.first_byte_ms != null ? <span className="zm-probe-time">{T.agent.firstByte((item.first_byte_ms / 1000).toFixed(1))}</span> : null}
                      {item.usable ? (
                        <Btn size="sm" busy={pickBusy === item.model} disabled={pickBusy !== null || probeBusy || resumeBusy} onClick={() => void pickAndResend(item.model, group.providerId)}>
                          {T.agent.useThisResend}
                        </Btn>
                      ) : null}
                    </li>
                  ))}
                </ul>
              )}
            </div>
          ))}
          {usable.length >= 2 ? <Btn size="sm" disabled={pickBusy !== null || probeBusy} onClick={pickFastest}>{T.agent.useAnyResend}</Btn> : null}
        </div>
      ) : null}
      {picked ? <p className="zm-failure-note">{T.agent.resendingWith(cleanModelName(picked))}</p> : null}
      <div className="zm-failure-actions">
        <Btn size="sm" variant="ghost" onClick={onOpenSettings}><Settings2 aria-hidden />模型设置</Btn>
        <Btn size="sm" variant="ghost" busy={probeBusy} disabled={resumeBusy || pickBusy !== null} onClick={() => void probe()}>{T.agent.testModels}</Btn>
      </div>
      </div> : null}
    </div>
  )
}

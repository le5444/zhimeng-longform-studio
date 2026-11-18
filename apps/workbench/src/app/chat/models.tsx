import { useId, useRef, useState } from 'react'
import { AlertCircle, ChevronDown, Info } from 'lucide-react'
import { agentSessionProviderPreference, type AgentSession } from '../../api/agent'
import { saveProviderProfile, saveProviderSettings, type ProviderContextWindow, type ProviderReasoningEffort, type ProviderSettingsView } from '../../api/providerSettings'
import { authorFacingError } from '../../lib/authorFacingError'
import { effortOptionLabel, resolveEffortFamily } from '../../lib/effortOptions'
import { contextWindowLabel } from '../../lib/modelContext'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'
import { T } from '../strings'
import type { Notify } from '../ui'

/** 模型名前面中转站加的「[分组]」去掉。 */
export function cleanModelName(model: string): string {
  return model.replace(/^\[[^\]]+\]\s*/, '')
}

/**
 * 输入框底下换模型、换思考强度、换上下文窗口：三个原生下拉，选中就存，下一句生效。
 * 「换了要不要告诉你」——作者 2026-10-09：成功别跳提示，失败才提示。
 * 没配模型就一个「去配置模型」。
 */
export function ModelControls({ provider, session, disabled, onSessionProviderChanged, onProviderChanged, onOpenSettings, notify }: {
  provider: ProviderSettingsApi
  session?: AgentSession | null
  disabled: boolean
  onSessionProviderChanged?: (settings: Pick<ProviderSettingsView, 'provider_id' | 'model' | 'reasoning_effort'>) => Promise<void>
  onProviderChanged: () => Promise<void>
  onOpenSettings: () => void
  notify: Notify
}) {
  const settings = provider.settings
  const preference = agentSessionProviderPreference(session)
  const currentProfile = provider.catalog?.providers.find((profile) => profile.provider_id === preference?.providerId)
  const usesActiveProvider = !preference?.providerId || preference.providerId === settings?.provider_id
  const currentSettings = currentProfile ?? (usesActiveProvider ? settings : null)
  const current = preference?.model ?? settings?.model
  const currentProviderId = preference?.providerId ?? settings?.provider_id
  const missingProvider = Boolean(preference?.providerId && provider.catalog && !currentProfile)
  const providerName = missingProvider ? '已删除的服务' : currentProfile?.name || (usesActiveProvider ? settings?.provider_name : null) || '服务名称未读取'
  const route = current ? currentProfile?.effective_routes?.[current] : undefined
  const routeText = route ? `${route.source === 'default' ? '模型建议 · 未验证' : route.source === 'verified' ? '已验证线路' : route.source === 'model_override' ? '模型例外' : '服务指定'} · ${route.wire_format === 'responses' ? 'Responses' : route.wire_format === 'anthropic_messages' ? 'Messages' : 'Chat'}` : null
  const identityUnavailable = Boolean(preference?.providerId && (!provider.catalog || provider.state === 'error'))
  const [busy, setBusy] = useState(false)
  const modelSelect = useRef<HTMLSelectElement>(null)
  const warningId = useId()
  if (!current || (!preference && !settings?.configured)) {
    return <button type="button" className="zm-composer-chip" data-model-pick="" onClick={onOpenSettings}>{T.agent.modelUnset}</button>
  }
  const models = [...new Set([current, ...(currentSettings?.selected_models ?? [])])]
  const otherProfiles = provider.catalog?.providers.filter((profile) => profile.provider_id !== currentProviderId) ?? []
  const effort: ProviderReasoningEffort = preference?.reasoningEffort ?? settings?.reasoning_effort ?? 'auto'
  const family = resolveEffortFamily(current).efforts
  const efforts = family.some((item) => item.value === effort) ? family : [...family, { value: effort, label: effortOptionLabel(effort), hint: '' }]

  const save = async (patch: { providerId?: string; model?: string; reasoningEffort?: ProviderReasoningEffort; contextWindow?: ProviderContextWindow }) => {
    setBusy(true)
    try {
      if (session && onSessionProviderChanged) {
        if (patch.contextWindow) {
          let saved = false
          try {
            if (currentProfile) {
              await saveProviderProfile({
                providerId: currentProfile.provider_id, name: currentProfile.name,
                model: currentProfile.model, selectedModels: currentProfile.selected_models,
                reasoningEffort: currentProfile.reasoning_effort,
                wireFormat: currentProfile.wire_format_explicit === false ? 'auto' : currentProfile.wire_format,
                acceptedReportedModels: currentProfile.accepted_reported_models,
                contextWindow: patch.contextWindow, makeActive: false,
              })
            } else if (usesActiveProvider) {
              await saveProviderSettings({ contextWindow: patch.contextWindow })
            } else {
              throw new Error('当前对话的模型服务尚未读取，请重新打开模型设置后再试')
            }
            saved = true
            await provider.reload({ throwOnError: true })
          } catch (error) {
            throw new Error(saved
              ? `历史容量已保存，但模型列表刷新失败。${authorFacingError(error, '请稍后重试')}`
              : authorFacingError(error, '历史容量保存失败'), { cause: error })
          }
        }
        // 容量属于服务，不需要重写当前对话没有改变的模型绑定。
        if (!patch.contextWindow) await onSessionProviderChanged({
          provider_id: patch.providerId ?? currentProviderId,
          model: patch.model ?? current,
          reasoning_effort: patch.reasoningEffort ?? effort,
        })
      } else {
        const chosenProfile = patch.providerId && patch.providerId !== currentProviderId
          ? provider.catalog?.providers.find((profile) => profile.provider_id === patch.providerId) : null
        if (chosenProfile) {
          await saveProviderProfile({
            providerId: chosenProfile.provider_id, name: chosenProfile.name,
            model: patch.model ?? chosenProfile.model, selectedModels: chosenProfile.selected_models,
            reasoningEffort: patch.reasoningEffort ?? chosenProfile.reasoning_effort,
            wireFormat: chosenProfile.wire_format_explicit === false ? 'auto' : chosenProfile.wire_format,
            acceptedReportedModels: chosenProfile.accepted_reported_models, makeActive: true,
          })
        } else await saveProviderSettings(patch)
        await onProviderChanged()
      }
      // 成功不打扰：上次会话/这次会话用的什么模型你看得见，不用每次都提醒一次
    } catch (error) {
      notify('danger', authorFacingError(error, T.agent.modelSwitchFailed))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="zm-model-controls">
      {missingProvider ? <div className="zm-model-warning" role="status" id={warningId}>
        <AlertCircle aria-hidden />
        <span>原模型服务已删除，请重新选择模型</span>
        {otherProfiles.some((profile) => profile.selected_models.length > 0)
          ? <button type="button" disabled={disabled || busy || identityUnavailable} onClick={() => {
            modelSelect.current?.focus()
            try { modelSelect.current?.showPicker?.() } catch { /* Keyboard selection remains available. */ }
          }}>选择可用模型</button>
          : <button type="button" onClick={onOpenSettings}>配置模型服务</button>}
      </div> : null}
      <div className="zm-model-fields">
      <div className="zm-compose-model-identity" role="group" aria-label="当前模型服务" title={`${providerName} / ${current}`}>
      <span className="zm-compose-provider" title={providerName}>{providerName}</span>
      <label className="zm-composer-chip zm-composer-select zm-compose-model-picker" title={current} data-model-pick="">
        <select ref={modelSelect} aria-label={T.agent.modelPick} aria-describedby={missingProvider ? warningId : undefined} value={current}
          onChange={(event) => {
            const value = event.target.value
            const other = otherProfiles.flatMap((profile) => profile.selected_models.map((model) => ({ profile, model })))
              .find(({ profile, model }) => JSON.stringify([profile.provider_id, model]) === value)
            if (other) void save({ providerId: other.profile.provider_id, model: other.model })
            else if (value !== current) void save({ model: value })
          }} disabled={disabled || busy || identityUnavailable}>
          <optgroup label={providerName}>
            {models.map((model) => <option key={model} value={model} disabled={missingProvider}>{cleanModelName(model)}</option>)}
          </optgroup>
          {otherProfiles.map((profile) => <optgroup key={profile.provider_id} label={profile.name}>
            {profile.selected_models.map((model) => <option key={model} value={JSON.stringify([profile.provider_id, model])}>{cleanModelName(model)}</option>)}
          </optgroup>)}
        </select><ChevronDown aria-hidden />
      </label>
      </div>
      <label className="zm-composer-chip zm-composer-select zm-compose-option" title={T.agent.effortPick}>
        <span>思考强度</span>
        <select
          aria-label={T.agent.effortPick}
          value={effort}
          onChange={(event) => { if (event.target.value !== effort) void save({ reasoningEffort: event.target.value as ProviderReasoningEffort }) }}
          disabled={disabled || busy || missingProvider || identityUnavailable}
        >
          {efforts.map((item) => (
            <option key={item.value} value={item.value}>{item.label}</option>
          ))}
        </select><ChevronDown aria-hidden />
      </label>

      {/* 换上下文窗口 */}
      {currentSettings ? <label className="zm-composer-chip zm-composer-select zm-compose-option" title={T.agent.contextWindowHint}>
        <span>容量</span>
        <select
          aria-label={T.agent.contextWindowPick}
          value={currentSettings.context_window ?? '256k'}
          onChange={(event) => { if (event.target.value !== (currentSettings.context_window ?? '256k')) void save({ contextWindow: event.target.value as ProviderContextWindow }) }}
          disabled={disabled || busy || identityUnavailable}
        >
          {(['256k', '1m'] as const).map((value) => (
            <option key={value} value={value}>{contextWindowLabel(value)}</option>
          ))}
        </select><ChevronDown aria-hidden />
      </label> : null}
      {route && routeText ? <button type="button" className="zm-composer-route" onClick={onOpenSettings} aria-label={`模型接口：${routeText}`} title={`${routeText} · 查看接口来源与验证状态`}>
        <Info aria-hidden />
      </button> : null}
      </div>
    </div>
  )
}

import type { ProviderAuthType, ProviderProfileView, ProviderReasoningEffort, ProviderWireFormat } from '../../api/providerSettings'
import { T } from '../strings'

export function wireName(wire: string): string {
  return T.settings.wireShort[wire] ?? wire
}

export type Busy = 'none' | 'fetching' | 'saving' | 'testingAll' | 'activating'

/** 右边表单现在打开的是哪一套：新的一套 / 已保存的某一套 / 什么都没开。 */
export type Selection = { kind: 'new' } | { kind: 'edit'; id: string } | null

export const WIRE_FORMATS: ProviderWireFormat[] = ['auto', 'responses', 'chat', 'anthropic_messages']
export const AUTH_TYPES: ProviderAuthType[] = ['bearer', 'x-api-key', 'custom', 'none']

/** 一套模型服务在表单里的样子。密钥只进不出：apiKey 只装作者这次新填的，留空就不动已保存的。 */
export interface ServiceForm {
  name: string
  baseUrl: string
  apiKey: string
  /** 表单里列出来的模型（拉到的 + 手填的 + 已保存的），勾选的是其中一部分。 */
  models: string[]
  selectedModels: string[]
  defaultModel: string
  reasoningEffort: ProviderReasoningEffort
  wireFormat: ProviderWireFormat
  authType: ProviderAuthType
  contextWindow: '256k' | '1m'
  extraHeadersText: string
  removedHeaderNames: string[]
  modelsUrl: string
  acceptedText: string
}

export type SingleTest = { ok: true; replyReceived: boolean; seconds: string; wireFormat?: string } | { ok: false; reason: string }

/** 只在当前表单仍是发起测试的连接时展示结果；这个标记仅留在组件内存。 */
export function connectionSignature(form: ServiceForm, providerId?: string) {
  const headers = parseHeaders(form.extraHeadersText)
  return JSON.stringify({
    providerId,
    baseUrl: form.baseUrl.trim(),
    apiKey: form.apiKey.trim(),
    wireFormat: form.wireFormat,
    authType: form.authType,
    headers: headers.ok ? Object.entries(headers.value ?? {}).sort(([a], [b]) => a.localeCompare(b)) : form.extraHeadersText,
    removedHeaders: form.removedHeaderNames.map((name) => name.toLowerCase()).sort(),
    accepted: splitLines(form.acceptedText).sort(),
  })
}

export function formSignature(form: ServiceForm) {
  const { models: _models, ...saved } = form
  return JSON.stringify(saved)
}

/** Preserve edits, while adopting fields this editor never changed. */
export function mergeModelDraft(form: ServiceForm, original: ServiceForm, latest: ServiceForm): ServiceForm {
  const merged = { ...latest }
  for (const key of Object.keys(form) as (keyof ServiceForm)[]) {
    if (JSON.stringify(form[key]) !== JSON.stringify(original[key])) Object.assign(merged, { [key]: form[key] })
  }
  merged.models = uniq([...latest.models, ...form.models])
  return merged
}

export function modelDraftDifferences(form: ServiceForm, latest: ProviderProfileView | null) {
  const saved = formFromProfile(latest)
  const labels: Partial<Record<keyof ServiceForm, string>> = {
    name: '服务名称', baseUrl: 'API 地址', defaultModel: '默认模型', selectedModels: '可选模型',
    reasoningEffort: '思考强度', wireFormat: '接口方式', authType: '认证方式', contextWindow: '历史容量',
    modelsUrl: '模型目录地址', acceptedText: '接受的模型名称',
  }
  const text = (value: unknown) => Array.isArray(value) ? value.join('、') : String(value ?? '')
  const rows = Object.entries(labels).flatMap(([key, label]) => {
    const field = key as keyof ServiceForm
    return text(form[field]) === text(saved[field]) ? [] : [{ label, saved: text(saved[field]), draft: text(form[field]) }]
  })
  if (form.apiKey || form.extraHeadersText || form.removedHeaderNames.length) rows.push({
    label: '密钥与请求头', saved: '保留最新保存的值', draft: '有待提交的替换或移除操作，内容不在比较中显示',
  })
  return rows
}

export interface LivenessSummary {
  usable: number
  tested: number
  fastest: string | null
}

export function uniq(items: string[]): string[] {
  return Array.from(new Set(items.map((item) => item.trim()).filter(Boolean)))
}

export function splitLines(text: string): string[] {
  return uniq(text.split(/[\n,，]/))
}

export function formFromProfile(profile: ProviderProfileView | null): ServiceForm {
  const selected = uniq([...(profile?.selected_models ?? []), ...(profile?.model ? [profile.model] : [])])
  const models = uniq([...selected, ...Object.keys(profile?.model_overrides ?? {})])
  return {
    name: profile?.name ?? '',
    baseUrl: profile?.base_url ?? '',
    apiKey: '',
    models,
    selectedModels: selected,
    defaultModel: profile?.model ?? models[0] ?? '',
    reasoningEffort: profile?.reasoning_effort ?? 'auto',
    // 新建服务、或加载到「没写过 wire_format」的服务，默认回显「自动」。
    // 渠道写过 wire_format 的，回显用户的选择（不被能力卡盖）。
    wireFormat: profile == null
      ? 'auto'
      : profile.wire_format_explicit === false
        ? 'auto'
        : profile.wire_format,
    authType: profile?.auth_type ?? 'bearer',
    contextWindow: profile?.context_window ?? '256k',
    extraHeadersText: '',
    removedHeaderNames: [],
    modelsUrl: profile?.models_url ?? '',
    acceptedText: (profile?.accepted_reported_models ?? []).join('\n'),
  }
}

/** 自定义请求头：每行「名字: 值」。整框留空 = 不传（后端保留已保存的）。 */
export function parseHeaders(text: string): { ok: true; value: Record<string, string> | undefined } | { ok: false; line: string } {
  if (!text.trim()) return { ok: true, value: undefined }
  const value: Record<string, string> = {}
  for (const raw of text.split('\n')) {
    const line = raw.trim()
    if (!line) continue
    const colon = line.search(/[:：]/)
    if (colon <= 0) return { ok: false, line }
    const name = line.slice(0, colon).trim()
    if (!name) return { ok: false, line }
    value[name] = line.slice(colon + 1).trim()
  }
  return { ok: true, value }
}

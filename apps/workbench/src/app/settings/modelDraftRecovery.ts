import type { ProviderProfileView } from '../../api/providerSettings'
import { AUTH_TYPES, WIRE_FORMATS, formFromProfile, type Selection, type ServiceForm } from './modelForm'
import { record, strings } from './draftRecovery'

export type ModelDraft = { form: ServiceForm; baseline: string; profile: ProviderProfileView | null }
export type ModelDraftSnapshot = { selection: Selection; drafts: Array<[string, ModelDraft]>; creationId: string }
const efforts = ['auto', 'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra']

function validForm(value: unknown, baseline = false): boolean {
  if (!record(value)) return false
  const shape = formFromProfile(null)
  return Object.entries(shape).every(([key, expected]) => baseline && key === 'models' ? true
    : Array.isArray(expected) ? strings(value[key]) : typeof value[key] === 'string')
    && WIRE_FORMATS.includes(value.wireFormat as never) && AUTH_TYPES.includes(value.authType as never)
    && efforts.includes(String(value.reasoningEffort))
    && ['256k', '1m'].includes(String(value.contextWindow))
}

function validOverrides(value: unknown): boolean {
  return value === undefined || (record(value) && Object.values(value).every(item => record(item)
    && strings(item.overridden_fields) && strings(item.extra_header_names) && typeof item.api_key_set === 'boolean'
    && (item.base_url == null || typeof item.base_url === 'string')
    && (item.wire_format == null || ['chat', 'responses', 'anthropic_messages'].includes(String(item.wire_format)))
    && (item.reasoning_effort == null || efforts.includes(String(item.reasoning_effort)))
    && (item.max_output_tokens == null || (typeof item.max_output_tokens === 'number' && Number.isFinite(item.max_output_tokens)))))
}

export function parseModelDraftSnapshot(value: unknown): ModelDraftSnapshot {
  if (!record(value) || !Array.isArray(value.drafts) || typeof value.creationId !== 'string'
    || !/^provider-[a-f0-9]{32}$/.test(value.creationId)) throw new Error('invalid model draft')
  if (value.selection !== null && (!record(value.selection) || !['new', 'edit'].includes(String(value.selection.kind))
    || (value.selection.kind === 'edit' && typeof value.selection.id !== 'string'))) throw new Error('invalid selection')
  const ids = new Set<string>()
  for (const entry of value.drafts) {
    if (!Array.isArray(entry) || entry.length !== 2 || typeof entry[0] !== 'string' || !record(entry[1])) throw new Error('invalid draft entry')
    if (ids.has(entry[0])) throw new Error('duplicate draft')
    ids.add(entry[0])
    const draft = entry[1]
    if (!validForm(draft.form) || typeof draft.baseline !== 'string' || !validForm(JSON.parse(draft.baseline), true)) throw new Error('invalid form')
    if (draft.profile !== null) {
      const profile = draft.profile
      if (!record(profile) || typeof profile.provider_id !== 'string' || profile.provider_id !== entry[0]
        || !['name', 'base_url', 'model', 'wire_format', 'reasoning_effort'].every(key => typeof profile[key] === 'string')
        || !strings(profile.selected_models) || !strings(profile.accepted_reported_models)
        || !['api_key_set', 'configured', 'active'].every(key => typeof profile[key] === 'boolean')
        || (profile.extra_header_names !== undefined && !strings(profile.extra_header_names))
        || (profile.revision !== undefined && typeof profile.revision !== 'string')
        || (profile.models_url !== undefined && typeof profile.models_url !== 'string')
        || !validOverrides(profile.model_overrides)
        || !validForm(formFromProfile(profile as unknown as ProviderProfileView))) throw new Error('invalid profile')
    } else if (entry[0] !== 'new') throw new Error('missing base profile')
  }
  return value as ModelDraftSnapshot
}

import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import { changedSettings, settingsRevisionQuery } from '../lib/settingsChanges'

/** 上下文窗口档位：256K（默认）/ 1M（最大）。 */
export type ProviderContextWindow = '256k' | '1m'

export interface ProviderSettingsView {
  revision?: string
  applies_to?: 'new_sessions'
  schema: 'zhimeng.provider-settings.v1'
  provider_id?: string | null
  provider_name?: string
  base_url: string
  model: string
  selected_models?: string[]
  reasoning_effort?: ProviderReasoningEffort
  wire_format?: ProviderWireFormat
  /** 允许中转把模型名报成这些别名之一；身份核对本身不放宽。 */
  accepted_reported_models?: string[]
  auth_type?: ProviderAuthType
  /** 自定义请求头只回名字，值只进不出。 */
  extra_header_names?: string[]
  models_url?: string
  model_overrides?: Record<string, ProviderModelOverrideView>
  /** 后端只告诉前端「有没有密钥」，永远不回传明文。 */
  api_key_set: boolean
  context_window?: ProviderContextWindow
  source: 'settings' | 'environment' | 'none'
  configured: boolean
}

export interface ProviderProfileView {
  revision?: string
  source?: 'settings' | 'environment'
  applies_to?: 'next_request'
  provider_id: string
  name: string
  base_url: string
  model: string
  selected_models: string[]
  reasoning_effort: ProviderReasoningEffort
  wire_format: ProviderWireFormat
  accepted_reported_models: string[]
  auth_type?: ProviderAuthType
  /** 自定义请求头只回名字，值只进不出。 */
  extra_header_names?: string[]
  models_url?: string
  model_overrides?: Record<string, ProviderModelOverrideView>
  context_window: ProviderContextWindow
  wire_format_explicit?: boolean
  /** 「自动」时测活在这条线路（地址+密钥）上真测通过的协议，按模型名。 */
  verified_routes?: Record<string, ProviderVerifiedRoute>
  effective_routes?: Record<string, { wire_format: Exclude<ProviderWireFormat, 'auto'>; source: 'model_override' | 'provider' | 'verified' | 'default' }>
  api_key_set: boolean
  configured: boolean
  active: boolean
}

/** 测活测出来的：这个模型在这条线路上走哪种协议、会不会调工具。 */
export interface ProviderVerifiedRoute {
  wire_format: Exclude<ProviderWireFormat, 'auto'>
  tools: boolean
  verified_at: string | null
}

/** 可回显的模型配置；密钥和请求头只返回是否设置与名字。 */
export interface ProviderModelOverrideView {
  overridden_fields: string[]
  api_key_set: boolean
  extra_header_names: string[]
  wire_format?: Exclude<ProviderWireFormat, 'auto'> | null
  base_url?: string | null
  reasoning_effort?: ProviderReasoningEffort | null
  max_output_tokens?: number | null
}

export interface ProviderModelOverridePatch {
  expected_revision?: string
  wire_format?: Exclude<ProviderWireFormat, 'auto'> | null
  base_url?: string | null
  reasoning_effort?: ProviderReasoningEffort | null
  max_output_tokens?: number | null
  api_key?: string
  inherit_api_key?: boolean
  extra_headers?: Record<string, string>
  remove_header_names?: string[]
  reset?: boolean
}

export function patchProviderModelOverride(providerId: string, model: string, patch: ProviderModelOverridePatch): Promise<ProviderCatalogView> {
  return changedSettings('providers', fetchRuntimeJson<ProviderCatalogView>(`/api/v1/settings/providers/${encodeURIComponent(providerId)}/model-overrides/${encodeURIComponent(model)}`, {}, {
    method: 'PATCH', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(patch),
  }))
}

export interface ProviderCatalogView {
  revision?: string
  active_settings?: ProviderSettingsView
  schema: 'zhimeng.provider-catalog.v1'
  active_provider_id: string | null
  providers: ProviderProfileView[]
}

export interface ProviderProfileSaveResult {
  catalog: ProviderCatalogView
  profile: ProviderProfileView
  active_settings: ProviderSettingsView
}

export type ProviderReasoningEffort = 'auto' | 'none' | 'low' | 'medium' | 'high' | 'xhigh' | 'max' | 'ultra'

/** 跟这个接口说哪种话。auto = 不写死，测活时按顺序真试、试通的记住；其余三种是作者明确选的。 */
export type ProviderWireFormat = 'chat' | 'responses' | 'anthropic_messages' | 'auto'

/** 请求时怎么带凭证。bearer 是大多数中转的默认；custom 表示只靠自定义请求头。 */
export type ProviderAuthType = 'bearer' | 'x-api-key' | 'custom' | 'none'

/** 一键测活：一个模型的实测结果分类。 */
export type ProviderLivenessStatus =
  | 'ok'
  | 'empty'
  | 'auth'
  | 'not_found'
  | 'upstream'
  | 'http_error'
  | 'timeout'
  | 'unreachable'

export interface ProviderLivenessResult {
  model: string
  status: ProviderLivenessStatus
  usable: boolean
  label: string
  note: string
  first_byte_ms: number | null
  elapsed_ms: number
  reply: string
  /** 这次结果走的协议；auto 表示是「自动」按顺序试出来的。 */
  wire_format?: Exclude<ProviderWireFormat, 'auto'>
  auto?: boolean
  /** 带一个只读探针工具问一句并回传合成结果：ok 是完整闭环，failed 是调出工具但第二轮没完成。 */
  tools?: 'ok' | 'no' | 'failed' | 'unsure' | 'untested'
  /** 是否真的收到过工具调用，以及结果回传后的第二轮是否完成。 */
  tool_call_emitted?: boolean
  tool_roundtrip?: 'ok' | 'failed' | 'not_attempted' | 'untested' | string
  tools_note?: string
  /** 按顺序试过的协议和各自结果。 */
  tried?: Array<{ wire_format: Exclude<ProviderWireFormat, 'auto'>; status: string; tools?: string; note?: string }>
  /** 「自动」时这次测出的协议有没有记下来（碰上说不准的失败、又只会出字的，不记）。 */
  remembered?: boolean
}

export interface ProviderLivenessResponse {
  schema: 'zhimeng.provider-model-liveness.v1'
  tested: number
  usable_count: number
  usable_models: string[]
  fastest_model: string | null
  results: ProviderLivenessResult[]
}

/** 探测模型列表要连外部服务，比普通接口慢，给足时间。 */
const PROBE_TIMEOUT_MS = 45_000

export function readProviderSettings(
  options: RuntimeClientOptions = {},
): Promise<ProviderSettingsView> {
  return fetchRuntimeJson('/api/v1/settings/provider', options)
}

export function readProviderCatalog(
  options: RuntimeClientOptions = {},
): Promise<ProviderCatalogView> {
  return fetchRuntimeJson('/api/v1/settings/providers', options)
}

/** 失败卡上的「认可这个名字」：把中转实际上报的模型名加进当前在用那套的接受名单。 */
export function acceptReportedModel(
  reportedModel: string,
  options: RuntimeClientOptions = {},
): Promise<{ already_accepted: boolean; reported_model: string }> {
  return fetchRuntimeJson('/api/v1/settings/provider/accept-reported-model', options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reported_model: reportedModel }),
  })
}

/** 作者本人点「显示已存的密钥」：只在这一个显式动作里从后端取明文。 */
export function revealProviderKey(providerId: string, options: RuntimeClientOptions = {}): Promise<{ api_key: string }> {
  return fetchRuntimeJson(`/api/v1/settings/providers/${encodeURIComponent(providerId)}/reveal-key`, options, {
    method: 'POST',
  })
}

export function saveProviderProfile(
  input: {
    expectedRevision?: string
    providerId?: string
    name: string
    baseUrl?: string
    model?: string
    selectedModels: string[]
    apiKey?: string
    reasoningEffort: ProviderReasoningEffort
    /** 'auto' 也照发：后端据此把以前明确选过的协议改回自动。 */
    wireFormat: ProviderWireFormat
    acceptedReportedModels: string[]
    authType?: ProviderAuthType
    /** 自定义请求头；不传 = 不修改已保存的头（值只进不出）。 */
    extraHeaders?: Record<string, string>
    removeHeaderNames?: string[]
    modelsUrl?: string
    /** 单个模型的覆盖；传了就以这份为准（每个模型内部仍是逐字段合并）。 */
    modelOverrides?: Record<
      string,
      {
        wire_format?: ProviderWireFormat
        base_url?: string
        api_key?: string
        reasoning_effort?: ProviderReasoningEffort
      }
    >
    contextWindow?: ProviderContextWindow
    makeActive: boolean
  },
  options: RuntimeClientOptions = {},
): Promise<ProviderProfileSaveResult> {
  return changedSettings('providers', fetchRuntimeJson<ProviderProfileSaveResult>('/api/v1/settings/providers', options, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider_id: input.providerId,
      expected_revision: input.expectedRevision,
      name: input.name,
      base_url: input.baseUrl,
      model: input.model,
      selected_models: input.selectedModels,
      api_key: input.apiKey,
      reasoning_effort: input.reasoningEffort,
      // 「自动」也明说：以前不发这个字段，后端接口默认填成 chat，选了自动却被存成「明确选了 Chat」。
      wire_format: input.wireFormat,
      accepted_reported_models: input.acceptedReportedModels,
      auth_type: input.authType,
      extra_headers: input.extraHeaders,
      remove_header_names: input.removeHeaderNames,
      models_url: input.modelsUrl,
      model_overrides: input.modelOverrides,
      context_window: input.contextWindow,
      make_active: input.makeActive,
    }),
  }))
}

export function activateProviderProfile(
  providerId: string,
  options: RuntimeClientOptions = {},
  expectedRevision?: string,
  expectedCatalogRevision?: string,
): Promise<ProviderSettingsView> {
  return changedSettings('providers', fetchRuntimeJson<ProviderSettingsView>(
    `/api/v1/settings/providers/${encodeURIComponent(providerId)}/activate${settingsRevisionQuery(expectedRevision, expectedCatalogRevision)}`,
    options,
    { method: 'POST' },
  ))
}

export function deleteProviderProfile(
  providerId: string,
  options: RuntimeClientOptions = {},
  expectedRevision?: string,
): Promise<ProviderCatalogView> {
  return changedSettings('providers', fetchRuntimeJson<ProviderCatalogView>(
    `/api/v1/settings/providers/${encodeURIComponent(providerId)}${settingsRevisionQuery(expectedRevision)}`,
    options,
    { method: 'DELETE' },
  ))
}

export function saveProviderSettings(
  input: { baseUrl?: string; model?: string; apiKey?: string; reasoningEffort?: ProviderReasoningEffort; contextWindow?: ProviderContextWindow; expectedRevision?: string },
  options: RuntimeClientOptions = {},
): Promise<ProviderSettingsView> {
  return changedSettings('providers', fetchRuntimeJson<ProviderSettingsView>('/api/v1/settings/provider', options, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      base_url: input.baseUrl,
      model: input.model,
      api_key: input.apiKey,
      reasoning_effort: input.reasoningEffort,
      context_window: input.contextWindow,
      expected_revision: input.expectedRevision,
    }),
  }))
}

/** 一键获取模型：只探测 /models，不调用模型生成，不产生费用。 */
export async function fetchProviderModels(
  baseUrl: string,
  apiKey: string | undefined,
  options: RuntimeClientOptions = {},
  providerId?: string,
  connection: { authType?: ProviderAuthType; extraHeaders?: Record<string, string>; removeHeaderNames?: string[]; modelsUrl?: string } = {},
): Promise<string[]> {
  const payload = await fetchRuntimeJson<{ models: string[] }>(
    '/api/v1/settings/provider/models',
    { timeoutMs: PROBE_TIMEOUT_MS, ...options },
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 不填密钥就让后端用已保存的，作者不必每次重新粘贴
      body: JSON.stringify({
        base_url: baseUrl,
        api_key: apiKey || undefined,
        provider_id: providerId,
        auth_type: connection.authType,
        extra_headers: connection.extraHeaders,
        remove_header_names: connection.removeHeaderNames,
        models_url: connection.modelsUrl,
      }),
    },
  )
  return payload.models
}

/** 一键测活：后端逐个真实试写一句，再用合成结果验证工具闭环；「自动」还要挨个试协议。 */
export function testProviderModelsLiveness(
  input: {
    providerId?: string
    baseUrl: string
    apiKey?: string
    models: string[]
    prompt?: string
    /** 表单里选的协议：具体的一种就只测它；auto 就按顺序试、测通的记住。 */
    wireFormat?: ProviderWireFormat
    authType?: ProviderAuthType
    extraHeaders?: Record<string, string>
    removeHeaderNames?: string[]
    acceptedReportedModels?: string[]
  },
  options: RuntimeClientOptions = {},
): Promise<ProviderLivenessResponse> {
  return fetchRuntimeJson('/api/v1/settings/provider/models/liveness', { timeoutMs: 600_000, ...options }, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider_id: input.providerId,
      base_url: input.baseUrl,
      api_key: input.apiKey || undefined,
      models: input.models,
      prompt: input.prompt || undefined,
      wire_format: input.wireFormat,
      auth_type: input.authType,
      extra_headers: input.extraHeaders,
      remove_header_names: input.removeHeaderNames,
      accepted_reported_models: input.acceptedReportedModels,
      test_scope: 'service_draft',
    }),
  })
}

/** 清空这条线路（地址+密钥）的测活记忆（「重测全部」时用：忘掉以前测通过的协议，从头再测）。 */
export async function clearVerifiedRoutes(
  input: { baseUrl: string; apiKey?: string; providerId?: string; authType?: ProviderAuthType; extraHeaders?: Record<string, string>; removeHeaderNames?: string[] },
  options: RuntimeClientOptions = {},
): Promise<{ cleared: number }> {
  return fetchRuntimeJson('/api/v1/settings/provider/routes', { timeoutMs: 15_000, ...options }, {
    method: 'DELETE',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      base_url: input.baseUrl,
      api_key: input.apiKey || undefined,
      provider_id: input.providerId || undefined,
      auth_type: input.authType,
      extra_headers: input.extraHeaders,
      remove_header_names: input.removeHeaderNames,
    }),
  })
}

export function testProviderModel(
  input: {
    providerId?: string
    baseUrl: string
    apiKey?: string
    model: string
    prompt: string
    wireFormat?: ProviderWireFormat
    authType?: ProviderAuthType
    extraHeaders?: Record<string, string>
    removeHeaderNames?: string[]
    acceptedReportedModels?: string[]
  },
  options: RuntimeClientOptions = {},
): Promise<{ schema: 'zhimeng.provider-test.v1'; ok: true; model: string; reply: string; latency_ms: number; wire_format?: Exclude<ProviderWireFormat, 'auto'> }> {
  return fetchRuntimeJson('/api/v1/settings/provider/test', { timeoutMs: 150_000, ...options }, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      provider_id: input.providerId,
      base_url: input.baseUrl,
      api_key: input.apiKey || undefined,
      model: input.model,
      prompt: input.prompt,
      wire_format: input.wireFormat,
      auth_type: input.authType,
      extra_headers: input.extraHeaders,
      remove_header_names: input.removeHeaderNames,
      accepted_reported_models: input.acceptedReportedModels,
      test_scope: 'service_draft',
    }),
  })
}

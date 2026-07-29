import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface ProviderSettingsView {
  schema: 'zhimeng.provider-settings.v1'
  base_url: string
  model: string
  /** 后端只告诉前端「有没有密钥」，永远不回传明文。 */
  api_key_set: boolean
  source: 'settings' | 'environment' | 'none'
  configured: boolean
}

/** 探测模型列表要连外部服务，比普通接口慢，给足时间。 */
const PROBE_TIMEOUT_MS = 45_000

export function readProviderSettings(
  options: RuntimeClientOptions = {},
): Promise<ProviderSettingsView> {
  return fetchRuntimeJson('/api/v1/settings/provider', options)
}

export function saveProviderSettings(
  input: { baseUrl?: string; model?: string; apiKey?: string },
  options: RuntimeClientOptions = {},
): Promise<ProviderSettingsView> {
  return fetchRuntimeJson('/api/v1/settings/provider', options, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({
      base_url: input.baseUrl,
      model: input.model,
      api_key: input.apiKey,
    }),
  })
}

/** 一键获取模型：只探测 /models，不调用模型生成，不产生费用。 */
export async function fetchProviderModels(
  baseUrl: string,
  apiKey: string | undefined,
  options: RuntimeClientOptions = {},
): Promise<string[]> {
  const payload = await fetchRuntimeJson<{ models: string[] }>(
    '/api/v1/settings/provider/models',
    { timeoutMs: PROBE_TIMEOUT_MS, ...options },
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      // 不填密钥就让后端用已保存的，作者不必每次重新粘贴
      body: JSON.stringify({ base_url: baseUrl, api_key: apiKey || undefined }),
    },
  )
  return payload.models
}

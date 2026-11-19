import type { AgentImageAttachment } from './agent'
import { fetchRuntimeJson, joinRuntimeUrl, RuntimeApiError, type RuntimeClientOptions } from './runtime'

export function isImageFile(file: File): boolean {
  return file.type.startsWith('image/') || /\.(png|jpe?g|gif|webp|bmp|avif|heic)$/i.test(file.name)
}

export async function uploadAgentImage(file: File, options: RuntimeClientOptions = {}): Promise<AgentImageAttachment> {
  const data = await new Promise<string>((resolve, reject) => {
    const reader = new FileReader()
    reader.onload = () => typeof reader.result === 'string' ? resolve(reader.result) : reject(new Error('图片读取失败'))
    reader.onerror = () => reject(new Error('图片读取失败，请重新添加'))
    reader.readAsDataURL(file)
  })
  return fetchRuntimeJson('/api/v1/agent/images', { timeoutMs: 120_000, ...options }, {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name: file.name || '粘贴的图片.png', data_url: data }),
  })
}

/** 图片请求携带本机认证，令牌不放 URL，也不写入消息/附件记录。 */
export async function readAgentImage(url: string, signal: AbortSignal): Promise<Blob> {
  if (!/^\/api\/v1\/agent\/(?:sessions\/[^/?#]+\/)?images\/[^/?#]+$/.test(url)) throw new Error('图片地址不属于本机附件记录')
  const headers = new Headers()
  const token = import.meta.env.VITE_RUNTIME_TOKEN ?? ''
  if (token) headers.set('Authorization', `Bearer ${token}`)
  const response = await fetch(joinRuntimeUrl(import.meta.env.VITE_RUNTIME_URL ?? '', url), { headers, signal })
  if (!response.ok) throw new RuntimeApiError(response.status, await response.json().catch(() => null))
  return response.blob()
}

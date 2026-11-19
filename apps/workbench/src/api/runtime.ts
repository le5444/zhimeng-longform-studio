export type RuntimeConnection = 'checking' | 'online' | 'degraded' | 'offline'

export interface RuntimeSnapshot {
  connection: RuntimeConnection
  ready: boolean
  serviceStatus: string
  runStatus: string | null
  version: string | null
  detail: string
  checkedAt: string
}

export interface RuntimeClientOptions {
  baseUrl?: string
  token?: string
  fetcher?: typeof fetch
  timeoutMs?: number
  signal?: AbortSignal
}

const DEFAULT_TIMEOUT_MS = 30_000
const HEALTH_TIMEOUT_MS = 3500

export class RuntimeApiError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(status: number, payload: unknown) {
    // 后端在响应体里给的是可操作的原因，例如
    // 「这次修改范围较大，请先查看修改范围」「删除文件前需要你确认」。
    // 原来 message 只有 `HTTP 409`，而 Studio 各页直接显示 err.message，
    // 作者屏幕上只剩一个状态码，真正的原因埋在 payload 里没人读。
    // 在这里取一次，全部调用点都受益（各 hook 里已有的 payload 提取写法照旧生效）。
    super(runtimeErrorReason(payload) ?? `HTTP ${status}`)
    this.name = 'RuntimeApiError'
    this.status = status
    this.payload = payload
  }
}

/** 从后端错误响应体里取出人话原因；取不到返回 null。 */
export function runtimeErrorReason(payload: unknown): string | null {
  const record = asRecord(payload)
  if (!record) return null
  const nested = asRecord(record.error)
  return firstString(nested?.message, record.detail, record.message)
}

/**
 * 等到超时被我们主动掐断的请求。
 *
 * 以前它和作者点「停止」走的是同一条 `controller.abort()`，界面只拿得到一个
 * AbortError，分不出「这台机器慢」和「我自己按了停止」，于是要么把作者的手
 * 说成服务故障，要么把真超时说成取消。单独一个类型，两层都能如实说。
 */
export class RuntimeTimeoutError extends Error {
  readonly timeoutMs: number

  constructor(timeoutMs: number) {
    super(`等这台电脑上的本机服务回音，超过 ${Math.max(1, Math.round(timeoutMs / 1000))} 秒没有等到`)
    this.name = 'RuntimeTimeoutError'
    this.timeoutMs = timeoutMs
  }
}

export class RuntimeResponseError extends Error {
  constructor(cause: unknown) {
    super('本机服务返回的数据不完整或格式不正确，请刷新核对操作结果。', { cause })
    this.name = 'RuntimeResponseError'
  }
}

/** 作者自己掐断的（点了停止、切走对话），不是出错。 */
export function isCancelledRequest(error: unknown): boolean {
  const name = (error as { name?: unknown } | null)?.name
  return name === 'AbortError'
}

function asRecord(value: unknown): Record<string, unknown> | null {
  return value !== null && typeof value === 'object'
    ? (value as Record<string, unknown>)
    : null
}

function firstString(...values: unknown[]): string | null {
  const value = values.find((item) => typeof item === 'string' && item.trim())
  return typeof value === 'string' ? value : null
}

export function joinRuntimeUrl(baseUrl: string, path: string): string {
  return `${baseUrl.replace(/\/$/, '')}${path}`
}

function healthIsReady(payload: unknown): boolean {
  const record = asRecord(payload)
  if (!record) return false
  if (record.ready === false) return false
  const status = firstString(record.status, record.state)
  return status ? ['ready', 'ok'].includes(status) : record.ready === true
}

// —— 并发 GET 在途去重（2026-09-12 任务 27）——
// 首屏多个组件同时挂载时各自拉一遍同一路径（实测 settings/provider ×2、
// 执行收据 ×2、章节批 ×2），作者等的是双倍网络往返。这里只合并「同时发起」
// 的同路径 GET：不缓存结果，后到的调用照常发新请求，不会有过期数据。
// 只合并默认请求。认证、超时及 RequestInit 的其他设置各自保留请求语义。
const inflightGets = new Map<string, Promise<unknown>>()

export async function fetchRuntimeJson<T>(
  path: string,
  options: RuntimeClientOptions = {},
  init: RequestInit = {},
): Promise<T> {
  const isPlainGet = (init.method ?? 'GET').toUpperCase() === 'GET' && !init.body
  const dedupable = isPlainGet
    && options.baseUrl === undefined
    && options.fetcher === undefined
    && options.signal === undefined
    && init.signal === undefined
    && options.token === undefined
    && options.timeoutMs === undefined
    && Object.keys(init).every((key) => key === 'method')
  if (!dedupable) {
    return fetchRuntimeJsonOnce<T>(path, options, init)
  }
  const existing = inflightGets.get(path)
  if (existing) {
    return existing as Promise<T>
  }
  const promise = fetchRuntimeJsonOnce<T>(path, options, init)
  inflightGets.set(path, promise)
  void promise
    .finally(() => {
      if (inflightGets.get(path) === promise) inflightGets.delete(path)
    })
    .catch(() => undefined)
  return promise
}

async function fetchRuntimeJsonOnce<T>(
  path: string,
  options: RuntimeClientOptions = {},
  init: RequestInit = {},
): Promise<T> {
  const baseUrl = options.baseUrl ?? import.meta.env.VITE_RUNTIME_URL ?? ''
  const token = options.token ?? import.meta.env.VITE_RUNTIME_TOKEN ?? ''
  const fetcher = options.fetcher ?? fetch
  const controller = new AbortController()
  const upstreamSignal = init.signal ?? options.signal
  const abortFromUpstream = () => controller.abort(upstreamSignal?.reason)
  if (upstreamSignal?.aborted) {
    abortFromUpstream()
  } else {
    upstreamSignal?.addEventListener('abort', abortFromUpstream, { once: true })
  }
  const timeoutMs = options.timeoutMs ?? DEFAULT_TIMEOUT_MS
  let timedOut = false
  const timer = window.setTimeout(() => {
    timedOut = true
    controller.abort()
  }, timeoutMs)

  try {
    const headers = new Headers(init.headers)
    if (!headers.has('Accept')) headers.set('Accept', 'application/json')
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetcher(joinRuntimeUrl(baseUrl, path), {
      ...init,
      signal: controller.signal,
      headers,
    })

    let payload: unknown = null
    if (response.status !== 204 && response.status !== 205) {
      try {
        payload = await response.json()
      } catch (error) {
        if (!response.ok) throw new RuntimeApiError(response.status, null)
        if (controller.signal.aborted || isCancelledRequest(error)) throw error
        throw new RuntimeResponseError(error)
      }
    }
    if (!response.ok) {
      throw new RuntimeApiError(response.status, payload)
    }

    return payload as T
  } catch (error) {
    // 后端已经回了具体状态码就照实说，别用「等太久」把真实原因盖掉。
    if (timedOut && !(error instanceof RuntimeApiError)) throw new RuntimeTimeoutError(timeoutMs)
    throw error
  } finally {
    window.clearTimeout(timer)
    upstreamSignal?.removeEventListener('abort', abortFromUpstream)
  }
}

async function requestSnapshotJson(
  path: string,
  options: RuntimeClientOptions,
): Promise<unknown> {
  return fetchRuntimeJson(path, options)
}

export async function fetchRuntimeSnapshot(
  options: RuntimeClientOptions = {},
): Promise<RuntimeSnapshot> {
  const [healthResult, statusResult] = await Promise.allSettled([
    requestSnapshotJson('/health/ready', { timeoutMs: HEALTH_TIMEOUT_MS, ...options }),
    requestSnapshotJson('/api/v1/runtime/status', { timeoutMs: HEALTH_TIMEOUT_MS, ...options }),
  ])

  if (healthResult.status === 'rejected' && statusResult.status === 'rejected') {
    return {
      connection: 'offline',
      ready: false,
      serviceStatus: 'offline',
      runStatus: null,
      version: null,
      detail: '本机 Runtime 未连接',
      checkedAt: new Date().toISOString(),
    }
  }

  const healthPayload =
    healthResult.status === 'fulfilled' ? healthResult.value : null
  const statusPayload =
    statusResult.status === 'fulfilled' ? statusResult.value : null
  const healthRecord = asRecord(healthPayload)
  const statusRecord = asRecord(statusPayload)
  const currentRun = asRecord(statusRecord?.current_run)
  const ready =
    healthResult.status === 'fulfilled' && healthIsReady(healthPayload)
  const connection: RuntimeConnection =
    ready && statusResult.status === 'fulfilled' && statusRecord !== null ? 'online' : 'degraded'

  return {
    connection,
    ready,
    serviceStatus:
      firstString(
        statusRecord?.status,
        statusRecord?.state,
        healthRecord?.status,
        healthRecord?.state,
      ) ?? (ready ? 'ready' : 'starting'),
    runStatus: firstString(
      currentRun?.status,
      currentRun?.state,
      statusRecord?.run_status,
    ),
    version: firstString(statusRecord?.version, healthRecord?.version),
    detail:
      firstString(statusRecord?.detail, healthRecord?.detail) ??
      (connection === 'online' ? 'Runtime 已就绪' : 'Runtime 尚未完全就绪'),
    checkedAt: new Date().toISOString(),
  }
}

export const initialRuntimeSnapshot: RuntimeSnapshot = {
  connection: 'checking',
  ready: false,
  serviceStatus: 'checking',
  runStatus: null,
  version: null,
  detail: '正在连接本机 Runtime',
  checkedAt: '',
}

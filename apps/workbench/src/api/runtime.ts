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
}

const DEFAULT_TIMEOUT_MS = 3500

export class RuntimeApiError extends Error {
  readonly status: number
  readonly payload: unknown

  constructor(status: number, payload: unknown) {
    super(`HTTP ${status}`)
    this.name = 'RuntimeApiError'
    this.status = status
    this.payload = payload
  }
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
  if (!record) return true
  if (record.ready === false) return false
  const status = firstString(record.status, record.state)
  return status ? !['not_ready', 'starting', 'blocked', 'error'].includes(status) : true
}

export async function fetchRuntimeJson<T>(
  path: string,
  options: RuntimeClientOptions = {},
  init: RequestInit = {},
): Promise<T> {
  const baseUrl = options.baseUrl ?? import.meta.env.VITE_RUNTIME_URL ?? ''
  const token = options.token ?? import.meta.env.VITE_RUNTIME_TOKEN ?? ''
  const fetcher = options.fetcher ?? fetch
  const controller = new AbortController()
  const upstreamSignal = init.signal
  const abortFromUpstream = () => controller.abort(upstreamSignal?.reason)
  if (upstreamSignal?.aborted) {
    abortFromUpstream()
  } else {
    upstreamSignal?.addEventListener('abort', abortFromUpstream, { once: true })
  }
  const timer = window.setTimeout(
    () => controller.abort(),
    options.timeoutMs ?? DEFAULT_TIMEOUT_MS,
  )

  try {
    const headers = new Headers(init.headers)
    if (!headers.has('Accept')) headers.set('Accept', 'application/json')
    if (token) headers.set('Authorization', `Bearer ${token}`)
    const response = await fetcher(joinRuntimeUrl(baseUrl, path), {
      ...init,
      signal: controller.signal,
      headers,
    })

    const payload = await response.json().catch(() => null)
    if (!response.ok) {
      throw new RuntimeApiError(response.status, payload)
    }

    return payload as T
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
    requestSnapshotJson('/health/ready', options),
    requestSnapshotJson('/api/v1/runtime/status', options),
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
    ready && statusResult.status === 'fulfilled' ? 'online' : 'degraded'

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

export interface DiagnosticsPackageReceipt {
  schema: 'zhimeng.diagnostics-package-receipt.v1'
  receipt_id: string
  export_relative_path: string
  formal_draft_allowed: false
  writes_project_truth: false
  contains_secrets: false
  contains_novel_body: false
  provider_api_key_present: boolean
  provider_api_key_redacted: boolean
  integrity_check: 'ok' | 'failed' | 'unavailable'
  created_at: string
  receipt_hash: string
}

export interface DiagnosticsPackageResponse {
  schema: 'zhimeng.diagnostics-package-response.v1'
  receipt: DiagnosticsPackageReceipt
  package: Record<string, unknown>
  not_claiming: string[]
}

export interface RuntimeBackupManifest {
  schema: 'zhimeng.runtime-backup-manifest.v1'
  backup_id: string
  created_at: string
  product_version: string
  environment: string
  database_schema_version: number
  journal_mode: string
  database_relative_path: string
  database_sha256: string
  database_size_bytes: number
  integrity_check: 'ok' | 'failed' | 'unavailable'
  foreign_key_violations: number
  includes_secrets: false
  includes_novel_body: false
  includes_project_files: false
  formal_draft_allowed: false
  policy_version: 'local-v1-runtime-backup-1'
  manifest_hash: string
}

export interface RuntimeBackupReceipt {
  schema: 'zhimeng.runtime-backup-receipt.v1'
  receipt_id: string
  backup_id: string
  outcome: 'created'
  backup_relative_path: string
  manifest: RuntimeBackupManifest
  formal_draft_allowed: false
  writes_project_truth: false
  created_at: string
  receipt_hash: string
}

export interface RuntimeBackupResponse {
  schema: 'zhimeng.runtime-backup-response.v1'
  receipt: RuntimeBackupReceipt
  not_claiming: string[]
}

export interface RuntimeBackupSummary {
  backup_id: string
  created_at: string
  database_schema_version: number
  database_sha256: string
  database_size_bytes: number
  integrity_check: 'ok' | 'failed' | 'unavailable'
  manifest_hash: string
  backup_relative_path: string
}

export interface RuntimeBackupListResponse {
  schema: 'zhimeng.runtime-backup-list.v1'
  backups: RuntimeBackupSummary[]
  not_claiming: string[]
}

export interface RuntimeRestoreRequest {
  schema?: 'zhimeng.runtime-restore-request.v1'
  backup_id: string
  expected_manifest_hash: string
}

export interface RuntimeRestoreReceipt {
  schema: 'zhimeng.runtime-restore-receipt.v1'
  receipt_id: string
  backup_id: string
  outcome: 'restored'
  restored_database_schema_version: number
  restored_database_sha256: string
  integrity_check: 'ok' | 'failed' | 'unavailable'
  foreign_key_violations: number
  pre_restore_backup_id: string | null
  formal_draft_allowed: false
  writes_project_truth: false
  includes_project_files: false
  created_at: string
  receipt_hash: string
}

export interface RuntimeRestoreResponse {
  schema: 'zhimeng.runtime-restore-response.v1'
  receipt: RuntimeRestoreReceipt
  not_claiming: string[]
}

export interface ReleaseReadinessCheck {
  code: string
  required: boolean
  present: boolean
  detail: string
}

export interface ReleaseReadinessReceipt {
  schema: 'zhimeng.local-v1-release-readiness.v1'
  status: 'ready' | 'not_ready'
  local_v1_ready: boolean
  local_v1_exe: boolean
  formal_draft_allowed: false
  quality_assessed: false
  four_skills_generic_exactly_once?: boolean
  checks: ReleaseReadinessCheck[]
  missing_required: string[]
  signed_package_status?: string
  signed_package_signature_ready?: boolean
  policy_version: string
  generated_at: string
  receipt_id: string
  receipt_hash: string
  runtime_surface?: string
  runtime_policy_version?: string
  evaluated_via?: string
  evaluated_at?: string
}

export interface ReleaseReadinessResponse {
  schema: 'zhimeng.local-v1-release-readiness-response.v1'
  receipt: ReleaseReadinessReceipt
  not_claiming: string[]
}

export function getReleaseReadiness(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<ReleaseReadinessResponse>(
    '/api/v1/runtime/release-readiness',
    options,
  )
}

export function evaluateReleaseReadiness(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<ReleaseReadinessResponse>(
    '/api/v1/runtime/release-readiness/evaluate',
    options,
    { method: 'POST' },
  )
}

export function exportDiagnosticsPackage(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<DiagnosticsPackageResponse>(
    '/api/v1/runtime/diagnostics-package',
    options,
    { method: 'POST' },
  )
}

export function listRuntimeBackups(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<RuntimeBackupListResponse>(
    '/api/v1/runtime/backups',
    options,
  )
}

export function createRuntimeBackup(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<RuntimeBackupResponse>(
    '/api/v1/runtime/backups',
    options,
    { method: 'POST' },
  )
}

export function restoreRuntimeBackup(
  request: RuntimeRestoreRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<RuntimeRestoreResponse>(
    '/api/v1/runtime/backups/restore',
    options,
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        schema: 'zhimeng.runtime-restore-request.v1',
        ...request,
      }),
    },
  )
}


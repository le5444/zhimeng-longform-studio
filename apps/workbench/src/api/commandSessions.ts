import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface CommandSessionSummary {
  command_session_id: string
  run_id: string
  command: string
  cwd: string
  status: 'running' | 'exited' | 'cancelled' | 'interrupted'
  exit_code: number | null
  created_at: number
  ended_at: number | null
  duration_ms: number
  stdout_bytes: number | null
  stderr_bytes: number | null
  output_error?: string | null
  timed_out: boolean
  interruption_reason: string | null
  transport?: 'pipes' | 'pty'
  terminal_rows?: number | null
  terminal_columns?: number | null
  terminal_windows_build?: number | null
  file_tracking?: {
    id?: string | null
    paths: string[]
    status: 'observing' | 'recorded' | 'partial' | 'unavailable' | 'unverified' | 'interrupted'
    problems: Array<{path:string; reason:string}>
    change_id?: string | null
    file_count?: number | null
    note?: string | null
  } | null
}

export interface CommandOutputPage {
  text: string
  start_offset: number
  next_offset: number
  total_bytes: number
  truncated_before: boolean
  has_more: boolean
}

export interface CommandSessionOutput extends CommandSessionSummary {
  stdout: CommandOutputPage
  stderr: CommandOutputPage
}

export interface CommandReadOptions {
  stdout_offset?: number
  stderr_offset?: number
  stdout_before?: number
  stderr_before?: number
  max_bytes?: number
}

function sessionPath(sessionId: string) {
  return `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/commands`
}

export function listCommandSessions(sessionId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<{ commands: CommandSessionSummary[] }>(sessionPath(sessionId), options)
}

export function readCommandSession(sessionId: string, commandId: string, cursor: CommandReadOptions = {}, options: RuntimeClientOptions = {}) {
  const query = new URLSearchParams()
  for (const [key, value] of Object.entries(cursor)) if (value !== undefined) query.set(key, String(value))
  const suffix = query.size ? `?${query}` : ''
  return fetchRuntimeJson<CommandSessionOutput>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}${suffix}`, options)
}

export function stopCommandSession(sessionId: string, commandId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<CommandSessionSummary>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}/stop`, options, { method: 'POST' })
}

export function inputCommandSession(sessionId: string, commandId: string, input: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<CommandSessionSummary>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}/input`, options,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ input }) })
}

export function resizeCommandSession(sessionId: string, commandId: string, rows: number, columns: number, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<CommandSessionSummary>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}/resize`, options,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ rows, columns }) })
}

export interface CommandRecoveryEntry {
  path: string
  revision: string
  action: 'restore' | 'remove'
  status: 'available' | 'unchanged' | 'conflict' | 'unavailable'
  before_sha256: string | null
  current_sha256: string | null
  before_text: string | null
  current_text: string | null
  note: string | null
}

export interface CommandRecoveryReceipt {
  request_id: string
  path: string
  change_id: string
  status: 'applied' | 'undone' | 'unconfirmed'
}

export interface CommandRecovery {
  observation_id: string
  source: 'command_end' | 'current_check'
  entries: CommandRecoveryEntry[]
  problems: string[]
  restorations: CommandRecoveryReceipt[]
  previewed_path: string | null
}

export interface CommandRecoveryRequest { path: string; revision: string; request_id: string }

export function inspectCommandRecovery(sessionId: string, commandId: string, path?: string, options: RuntimeClientOptions = {}) {
  const query = path ? `?${new URLSearchParams({ path })}` : ''
  return fetchRuntimeJson<CommandRecovery>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}/file-recovery${query}`, options)
}

export function restoreCommandFile(sessionId: string, commandId: string, request: CommandRecoveryRequest, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<CommandRecoveryReceipt>(`${sessionPath(sessionId)}/${encodeURIComponent(commandId)}/file-recovery`, options,
    { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(request) })
}

import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import { changedSettings, settingsRevisionQuery } from '../lib/settingsChanges'

export interface McpServerView {
  revision?: string
  applied_revision?: string | null
  configuration_status?: 'applied' | 'pending'
  apply_error?: string | null
  source?: 'settings'
  connection_id: string
  name: string
  transport?: 'stdio' | 'http'
  url?: string | null
  header_names?: string[]
  command: string
  args: string[]
  cwd: string | null
  enabled: boolean
  env_names: string[]
  state: 'disabled' | 'connecting' | 'ready' | 'error'
  error: string | null
  tool_count: number
  protocol_version: string | null
}

export interface McpServerUpdate {
  expected_revision?: string
  creation_id?: string
  name: string
  transport?: 'stdio' | 'http'
  url?: string | null
  headers?: Record<string, string>
  remove_headers?: string[]
  command: string
  args: string[]
  cwd: string | null
  enabled: boolean
  env: Record<string, string>
  remove_env: string[]
}

export interface McpToolView {
  alias: string
  raw_name: string
  title?: string | null
  description: string
  input_schema: Record<string, unknown>
  read_only: boolean
  annotations?: Record<string, unknown> | null
  schema_error?: string | null
}

const path = '/api/v1/mcp/servers'
const serverPath = (id: string) => `${path}/${encodeURIComponent(id)}`
const json = (method: string, body: unknown): RequestInit => ({ method, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) })

export function listMcpServers(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<{ servers: McpServerView[] }>(path, options)
}

export function createMcpServer(body: McpServerUpdate, options: RuntimeClientOptions = {}) {
  return changedSettings('mcp', fetchRuntimeJson<McpServerView>(path, options, json('POST', body)))
}

export function updateMcpServer(id: string, body: McpServerUpdate, options: RuntimeClientOptions = {}) {
  return changedSettings('mcp', fetchRuntimeJson<McpServerView>(serverPath(id), options, json('PUT', body)))
}

export function deleteMcpServer(id: string, options: RuntimeClientOptions = {}, expectedRevision?: string) {
  return changedSettings('mcp', fetchRuntimeJson<void>(`${serverPath(id)}${settingsRevisionQuery(expectedRevision)}`, options, { method: 'DELETE' }))
}

export function setMcpServerEnabled(id: string, enabled: boolean, options: RuntimeClientOptions = {}, expectedRevision?: string) {
  return changedSettings('mcp', fetchRuntimeJson<McpServerView>(`${serverPath(id)}/enabled`, options, json('PATCH', { enabled, expected_revision: expectedRevision })))
}

export function reconnectMcpServer(id: string, options: RuntimeClientOptions = {}, expectedRevision?: string) {
  return changedSettings('mcp', fetchRuntimeJson<McpServerView>(`${serverPath(id)}/reconnect${settingsRevisionQuery(expectedRevision)}`, options, { method: 'POST' }))
}

export function listMcpTools(id: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<{ tools: McpToolView[] }>(`${serverPath(id)}/tools`, options)
}

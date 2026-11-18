import { record, strings } from './draftRecovery'
import type { Draft } from './mcpForm'

export type McpDraftSnapshot = { selected: string | null; drafts: Record<string, Draft>; importText: string; importOpen: boolean }

function secretRows(value: unknown): boolean {
  return Array.isArray(value) && value.every(row => record(row) && ['key', 'name', 'value'].every(key => typeof row[key] === 'string'))
    && new Set(value.map(row => row.key)).size === value.length
}

export function parseMcpDraftSnapshot(value: unknown): McpDraftSnapshot {
  if (!record(value) || !record(value.drafts) || typeof value.importText !== 'string' || typeof value.importOpen !== 'boolean'
    || (value.selected !== null && typeof value.selected !== 'string')) throw new Error('invalid MCP draft')
  for (const [id, draft] of Object.entries(value.drafts)) {
    if (!record(draft) || !['name', 'url', 'command', 'args', 'cwd', 'original'].every(key => typeof draft[key] === 'string')
      || !['http', 'stdio'].includes(String(draft.transport)) || !strings(draft.removed) || !strings(draft.removedHeaders)
      || !secretRows(draft.env) || !secretRows(draft.headers)
      || (draft.revision !== undefined && typeof draft.revision !== 'string')
      || (draft.enabled !== undefined && typeof draft.enabled !== 'boolean')
      || (draft.creationId !== undefined && (typeof draft.creationId !== 'string' || !/^mcp-[a-f0-9]{32}$/.test(draft.creationId)))) throw new Error('invalid MCP form')
    if (draft.base !== undefined && (!record(draft.base) || draft.base.connection_id !== id
      || !['name', 'command'].every(key => typeof (draft.base as Record<string, unknown>)[key] === 'string')
      || !strings(draft.base.args) || !strings(draft.base.env_names)
      || (draft.base.transport !== undefined && !['stdio', 'http'].includes(String(draft.base.transport)))
      || (draft.base.url != null && typeof draft.base.url !== 'string')
      || (draft.base.cwd != null && typeof draft.base.cwd !== 'string')
      || typeof draft.base.enabled !== 'boolean'
      || (draft.base.header_names !== undefined && !strings(draft.base.header_names)))) throw new Error('invalid MCP base')
    if (draft.base === undefined && id !== 'new' && !id.startsWith('import:')) throw new Error('missing MCP base')
  }
  return value as McpDraftSnapshot
}

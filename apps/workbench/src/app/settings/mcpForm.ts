import type { McpServerUpdate, McpServerView } from '../../api/mcp'

export type SecretRow = { key: string; name: string; value: string }
export type Draft = { name: string; transport: 'stdio' | 'http'; url: string; command: string; args: string; cwd: string; env: SecretRow[]; removed: string[]; headers: SecretRow[]; removedHeaders: string[]; original: string; enabled?: boolean; revision?: string; base?: McpServerView; creationId?: string }
export const fingerprint = (draft: Omit<Draft, 'original'> | Draft) => JSON.stringify([draft.name, draft.transport, draft.url, draft.command, draft.args, draft.cwd, draft.env, draft.removed, draft.headers, draft.removedHeaders])

export function makeDraft(server?: McpServerView): Draft {
  const draft: Omit<Draft, 'original'> = { name: server?.name ?? '', transport: server?.transport ?? 'stdio', url: server?.url ?? '', command: server?.command ?? '', args: server?.args.length ? JSON.stringify(server.args, null, 2) : '', cwd: server?.cwd ?? '', env: [], removed: [], headers: [], removedHeaders: [] }
  return { ...draft, original: fingerprint(draft), revision: server?.revision, base: server, enabled: server?.enabled,
    creationId: server ? undefined : `mcp-${crypto.randomUUID().replaceAll('-', '')}` }
}

const draftFields = ['name', 'transport', 'url', 'command', 'args', 'cwd', 'env', 'removed', 'headers', 'removedHeaders'] as const
export function mergeDraft(draft: Draft, server: McpServerView): Draft {
  const next = makeDraft(server)
  const original = makeDraft(draft.base)
  for (const key of draftFields) {
    if (JSON.stringify(draft[key]) !== JSON.stringify(original[key])) Object.assign(next, { [key]: draft[key] })
  }
  return next
}

export function differences(draft: Draft, server?: McpServerView) {
  const latest = makeDraft(server)
  const labels = { name: '服务名称', transport: '连接方式', url: '服务地址', command: '启动程序', args: '启动参数', cwd: '工作目录' }
  const rows = Object.entries(labels).flatMap(([key, label]) => {
    const field = key as keyof typeof labels
    return latest[field] === draft[field] ? [] : [{ label, saved: latest[field], draft: draft[field] }]
  })
  if (draft.env.length || draft.headers.length || draft.removed.length || draft.removedHeaders.length) rows.push({
    label: '变量与请求头', saved: '保留最新保存的值', draft: '有待提交的替换或移除操作，内容不在比较中显示',
  })
  return rows
}

/** Arguments are separate values, never parsed as a shell command. */
export function parseMcpArgs(text: string): string[] {
  if (!text.trim()) return []
  if (text.trimStart().startsWith('[')) {
    let value: unknown
    try { value = JSON.parse(text) } catch { throw new Error('参数的 JSON 格式有误，请填写字符串数组，或改为每行一个参数。') }
    if (!Array.isArray(value) || value.some((part) => typeof part !== 'string')) throw new Error('参数的 JSON 数组中，每一项都应是文字。')
    return value
  }
  return text.split(/\r?\n/).filter((line) => line.trim().length > 0)
}

function secretValues(rows: SecretRow[], kind: 'env' | 'headers'): Record<string, string> {
  const values: Record<string, string> = {}
  const names = new Set<string>()
  const label = kind === 'env' ? '环境变量' : '请求头'
  for (const row of rows) {
    const name = row.name.trim()
    if (!name && !row.value) continue
    if (!name) throw new Error(`请填写${label}名称。`)
    const key = kind === 'headers' ? name.toLowerCase() : name
    if (names.has(key)) throw new Error(`${label} ${name} 重复了，请合并为一项。`)
    names.add(key)
    values[name] = row.value
  }
  return values
}

function validHttpUrl(value: string): boolean {
  try { return ['http:', 'https:'].includes(new URL(value.trim()).protocol) } catch { return false }
}

export function updateBody(draft: Draft, enabled: boolean, previous?: McpServerView): McpServerUpdate {
  if (!draft.name.trim()) throw new Error('请填写服务名称。')
  const http = draft.transport === 'http'
  if (http) {
    if (!draft.url.trim()) throw new Error('请填写服务地址。')
    if (!validHttpUrl(draft.url)) throw new Error('服务地址需要是完整的 http:// 或 https:// 地址。')
  } else if (!draft.command.trim()) throw new Error('请填写启动程序。')
  let args: string[]
  try { args = parseMcpArgs(draft.args) } catch (error) {
    if (!http) throw error
    args = previous?.args ?? []
  }
  const url = draft.url.trim() ? validHttpUrl(draft.url) ? draft.url.trim() : previous?.url ?? null : null
  return { ...(draft.creationId ? { creation_id: draft.creationId, expected_revision: 'missing' } : draft.revision ? { expected_revision: draft.revision } : {}), name: draft.name.trim(), transport: draft.transport, url, command: draft.command.trim(), args, cwd: draft.cwd.trim() || null, enabled,
    env: http ? {} : secretValues(draft.env, 'env'), remove_env: http ? [] : draft.removed,
    headers: http ? secretValues(draft.headers, 'headers') : {}, remove_headers: http ? draft.removedHeaders : [] }
}

export function afterSave(draft: Draft, server: McpServerView): Draft {
  const next = makeDraft(server)
  // The inactive transport's credentials have not been submitted; retain that draft.
  if (draft.transport === 'http') {
    next.env = draft.env
    next.removed = draft.removed
    try { parseMcpArgs(draft.args) } catch { next.args = draft.args }
  } else {
    next.headers = draft.headers
    next.removedHeaders = draft.removedHeaders
    if (draft.url.trim() && !validHttpUrl(draft.url)) next.url = draft.url
  }
  return next
}

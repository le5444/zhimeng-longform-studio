import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

/**
 * 记忆：关于这个人的事，每个对话（没打开文件夹的也一样）一开头 Agent 就看得到（纠偏指令 3.7）。
 *
 * 存在本机状态目录的《用户记忆.md》，一行一条。开着的时候 Agent 自己往里记（9-29 起不用等你说「记住」）；
 * 设置页「记忆」能关掉、逐条改删、也能整份直接改。关着就不记、也不带。
 * 项目记忆是打开的文件夹里的 AGENTS.md，就是项目里的普通文件，不走这里。
 */

export interface UserMemoryView {
  schema: 'zhimeng.user-memory.v1'
  path: string
  exists: boolean
  content: string
  /** 读到时内容的 sha256：存的时候带回去，这中间 Agent 又记了一条就不会被盖掉（后台回 409）。 */
  sha256: string
  /** 记忆开没开（设置页的开关）。 */
  enabled: boolean
  /** 开关不可读时暂停使用，保留错误事实；旧服务可能没有此字段。 */
  settings_error?: string | null
  /** 项目记忆记在哪个文件里（打开的文件夹根目录下）。 */
  project_memory_file: string
  updated_at?: string | null
}

export interface MemoryVersion { version: string; saved_at: string; characters: number }
export interface MemoryHistory { items: MemoryVersion[]; next_offset: number | null }
export function readMemoryHistory(offset = 0): Promise<MemoryHistory> {
  return fetchRuntimeJson(`/api/v1/memory/user/history?offset=${offset}`)
}
export function readMemoryVersion(version: string): Promise<{ version: string; saved_at: string; content: string }> {
  return fetchRuntimeJson(`/api/v1/memory/user/history/${encodeURIComponent(version)}`)
}

export function readUserMemory(
  options: RuntimeClientOptions = {},
): Promise<UserMemoryView> {
  return fetchRuntimeJson('/api/v1/memory/user', options)
}

export function saveUserMemory(
  content: string,
  expectedSha256: string | null,
  options: RuntimeClientOptions = {},
): Promise<UserMemoryView & { ok: boolean }> {
  return fetchRuntimeJson('/api/v1/memory/user', options, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(expectedSha256 ? { content, expected_sha256: expectedSha256 } : { content }),
  })
}

export function saveMemoryEnabled(
  enabled: boolean,
  options: RuntimeClientOptions = {},
): Promise<UserMemoryView> {
  return fetchRuntimeJson('/api/v1/memory/user/settings', options, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ enabled }),
  })
}

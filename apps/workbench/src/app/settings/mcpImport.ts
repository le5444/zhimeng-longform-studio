import type { McpServerUpdate } from '../../api/mcp'

function object(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

function strings(value: unknown, label: string): Record<string, string> {
  if (value === undefined) return {}
  if (!object(value) || Object.values(value).some((item) => typeof item !== 'string')) {
    throw new Error(`${label}需要是名称与文字值组成的对象。`)
  }
  return value as Record<string, string>
}

/** Parse only. Loading a configuration never starts a process or saves a secret. */
export function parseMcpImport(text: string): McpServerUpdate[] {
  let root: unknown
  try { root = JSON.parse(text) } catch { throw new Error('JSON 格式有误，请粘贴完整的 MCP 配置。') }
  if (!object(root)) throw new Error('MCP 配置需要是一个 JSON 对象。')
  const entries = root.mcpServers
  if (!object(entries) || Object.keys(entries).length === 0) throw new Error('请提供包含至少一项服务的 mcpServers 对象。')
  const rootExtras = Object.keys(root).filter((key) => key !== 'mcpServers')
  if (rootExtras.length) throw new Error(`请只粘贴 MCP 配置；这些顶层字段不会导入：${rootExtras.join('、')}。`)
  return Object.entries(entries).map(([name, value]) => {
    if (!name.trim() || !object(value)) throw new Error(`服务 ${name || '（未命名）'} 的配置需要是对象。`)
    const label = `服务「${name}」：`
    const supported = ['name', 'type', 'transport', 'command', 'args', 'cwd', 'env', 'url', 'headers', 'disabled', 'enabled']
    const extras = Object.keys(value).filter((key) => !supported.includes(key))
    if (extras.length) throw new Error(`${label}暂不支持字段 ${extras.join('、')}，请先确认其用途并调整配置。`)
    const type = value.type ?? value.transport ?? (value.url ? 'http' : 'stdio')
    if (value.type && value.transport && value.type !== value.transport) throw new Error(`${label}type 和 transport 不一致。`)
    if (!['stdio', 'http', 'streamableHttp', 'streamable-http'].includes(String(type))) {
      throw new Error(`${label}暂不支持 ${String(type)} 连接，请使用本地程序或 Streamable HTTP 地址。`)
    }
    for (const field of ['name', 'command', 'cwd', 'url']) {
      if (value[field] !== undefined && typeof value[field] !== 'string') throw new Error(`${label}${field} 需要是文字。`)
    }
    if (value.args !== undefined && (!Array.isArray(value.args) || value.args.some((arg) => typeof arg !== 'string'))) {
      throw new Error(`${label}args 需要是字符串数组，含空格的路径放在同一个元素中。`)
    }
    for (const field of ['enabled', 'disabled']) {
      if (value[field] !== undefined && typeof value[field] !== 'boolean') throw new Error(`${label}${field} 需要是 true 或 false。`)
    }
    if (typeof value.enabled === 'boolean' && typeof value.disabled === 'boolean' && value.enabled === value.disabled) {
      throw new Error(`${label}enabled 和 disabled 相互矛盾。`)
    }
    const http = type !== 'stdio'
    const url = typeof value.url === 'string' ? value.url.trim() : ''
    const command = typeof value.command === 'string' ? value.command.trim() : ''
    const env = strings(value.env, `${label}env`)
    const headers = strings(value.headers, `${label}headers`)
    if (http) {
      try { if (!['http:', 'https:'].includes(new URL(url).protocol)) throw new Error() }
      catch { throw new Error(`${label}需要完整的 http:// 或 https:// 服务地址。`) }
      if (command || value.args || value.cwd || Object.keys(env).length) throw new Error(`${label}网络连接中包含本地程序参数，请选择一种连接方式。`)
    } else {
      if (!command) throw new Error(`${label}缺少启动程序 command。`)
      if (url || Object.keys(headers).length) throw new Error(`${label}本地程序中包含网络地址或请求头，请选择一种连接方式。`)
    }
    return {
      name: typeof value.name === 'string' && value.name.trim() ? value.name.trim() : name.trim(),
      transport: http ? 'http' : 'stdio', url: http ? url : null, command,
      args: value.args as string[] ?? [], cwd: typeof value.cwd === 'string' ? value.cwd : null,
      env, headers, remove_env: [], remove_headers: [],
      enabled: typeof value.enabled === 'boolean' ? value.enabled : value.disabled !== true,
    }
  })
}

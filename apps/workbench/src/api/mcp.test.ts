import { describe, expect, it, vi } from 'vitest'
import { createMcpServer, deleteMcpServer, listMcpServers, listMcpTools, reconnectMcpServer, setMcpServerEnabled, updateMcpServer, type McpServerUpdate } from './mcp'

const body: McpServerUpdate = { name: '本地资料', command: 'C:\\Program Files\\node.exe', args: ['C:\\目录 名\\mcp.js', '--stdio'], cwd: null, enabled: true, env: { TOKEN: 'new-secret' }, remove_env: ['OLD_TOKEN'] }

describe('MCP 服务 API', () => {
  it('创建和编辑逐项传递参数与变量，只对指定服务写入', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}'))
    await createMcpServer(body, { fetcher, token: 'local-token', baseUrl: 'http://127.0.0.1:8788' })
    await updateMcpServer('service /1', body, { fetcher })
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8788/api/v1/mcp/servers')
    expect(fetcher.mock.calls[0][1]?.method).toBe('POST')
    expect(new Headers(fetcher.mock.calls[0][1]?.headers).get('Authorization')).toBe('Bearer local-token')
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toEqual(body)
    expect(fetcher.mock.calls[1][0]).toBe('/api/v1/mcp/servers/service%20%2F1')
    expect(fetcher.mock.calls[1][1]?.method).toBe('PUT')
  })

  it('列表与工具请求只读，启停与重连分别操作，删除接受空响应', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => init?.method === 'DELETE' ? new Response(null, { status: 204 }) : new Response('{}'))
    const controller = new AbortController()
    await listMcpServers({ fetcher, signal: controller.signal })
    await listMcpTools('a/b', { fetcher })
    await setMcpServerEnabled('a/b', false, { fetcher })
    await reconnectMcpServer('a/b', { fetcher })
    await expect(deleteMcpServer('a/b', { fetcher })).resolves.toBeNull()
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual(['/api/v1/mcp/servers', '/api/v1/mcp/servers/a%2Fb/tools', '/api/v1/mcp/servers/a%2Fb/enabled', '/api/v1/mcp/servers/a%2Fb/reconnect', '/api/v1/mcp/servers/a%2Fb'])
    expect(fetcher.mock.calls[0][1]?.method).toBeUndefined()
    expect(fetcher.mock.calls[0][1]?.signal).toBeInstanceOf(AbortSignal)
    expect(fetcher.mock.calls[2][1]?.method).toBe('PATCH')
    expect(JSON.parse(fetcher.mock.calls[2][1]?.body as string)).toEqual({ enabled: false })
    expect(fetcher.mock.calls[3][1]?.method).toBe('POST')
  })

  it('错误保留服务的实际原因', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ detail: '启动程序不存在' }), { status: 422 }))
    await expect(createMcpServer(body, { fetcher })).rejects.toThrow('启动程序不存在')
  })

  it('HTTP服务地址和请求头只送给本机配置端点，返回只读取名称', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify({ connection_id: 'http-1', transport: 'http', url: 'https://mcp.example.test/tools', header_names: ['Authorization'] })))
    const saved = await createMcpServer({ ...body, transport: 'http', command: '', args: [], url: 'https://mcp.example.test/tools', headers: { authorization: 'Bearer synthetic-test-value' }, remove_headers: ['X-Old'] }, { fetcher })
    expect(fetcher.mock.calls[0][0]).toBe('/api/v1/mcp/servers')
    expect(JSON.parse(fetcher.mock.calls[0][1]?.body as string)).toMatchObject({ transport: 'http', url: 'https://mcp.example.test/tools', headers: { authorization: 'Bearer synthetic-test-value' }, remove_headers: ['X-Old'] })
    expect(saved.header_names).toEqual(['Authorization'])
    expect(JSON.stringify(saved)).not.toContain('synthetic-test-value')
  })
})

import { expect, it } from 'vitest'
import { parseMcpImport } from '../settings/mcpImport'

it('本地和 HTTP 导入保留带空格路径、秘密值和显式停用状态', () => {
  const parsed = parseMcpImport(JSON.stringify({ mcpServers: {
    local: { command: 'C:\\Program Files\\app.exe', args: ['a b', '--json'], env: { TOKEN: 'test-value' }, disabled: true },
    remote: { type: 'streamableHttp', url: 'https://example.com/mcp', headers: { Authorization: 'Bearer test-value' } },
  } }))
  expect(parsed[0]).toMatchObject({ name: 'local', command: 'C:\\Program Files\\app.exe', args: ['a b', '--json'], env: { TOKEN: 'test-value' }, enabled: false })
  expect(parsed[1]).toMatchObject({ name: 'remote', transport: 'http', url: 'https://example.com/mcp', headers: { Authorization: 'Bearer test-value' }, enabled: true })
})

it.each([
  ['broken', 'JSON'],
  ['{"mcpServers":{}}', '至少一项'],
  [JSON.stringify({ mcpServers: { a: { command: 'node', unknown: 'value' } } }), '暂不支持字段'],
  [JSON.stringify({ mcpServers: { a: { type: 'sse', url: 'https://example.com' } } }), '暂不支持 sse'],
  [JSON.stringify({ mcpServers: { a: { command: 'node', args: [1] } } }), '字符串数组'],
  [JSON.stringify({ mcpServers: { a: { command: 'node', enabled: true, disabled: true } } }), '相互矛盾'],
  [JSON.stringify({ mcpServers: { a: { url: 'file:///tmp/x' } } }), 'http://'],
  [JSON.stringify({ mcpServers: { a: { url: 'https://example.com', command: 'node' } } }), '一种连接方式'],
])('格式或能力不兼容时说明具体问题，避免静默丢配置：%s', (input, error) => {
  expect(() => parseMcpImport(input)).toThrow(error)
})

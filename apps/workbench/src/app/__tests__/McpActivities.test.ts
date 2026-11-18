import { expect, it } from 'vitest'
import type { AgentActivity } from '../../api/agent'
import { actionWords } from '../lib/words'
import { agentFileEffects } from '../lib/agentFileEffects'

const alias = 'mcp__write_summary_' + 'a'.repeat(32)
function events(output: Record<string, unknown>, failed = false): AgentActivity[] {
  return [
    { activity_id: 'call', run_id: 'run', session_id: 'session', sequence: 1, kind: 'tool_call', payload: { tool_call_id: 'tool', tool_name: alias, arguments: {} } },
    { activity_id: 'result', run_id: 'run', session_id: 'session', sequence: 2, kind: 'tool_result', payload: {
      tool_call_id: 'tool', status: failed ? 'failed' : 'succeeded', output,
      error: failed ? { error: { code: 'agent_mcp_tool_error', message: '工具报告失败' } } : null,
    } },
  ] as unknown as AgentActivity[]
}
const metadata = { schema: 'zhimeng.tool-result.mcp.v1', source_mcp: true, server: '本地表格', tool: 'write.summary', title: '生成汇总', read_only: false }

it('MCP results show the service and original tool title with text and structured content', () => {
  const rows = actionWords(events({ ...metadata, content: [{ type: 'text', text: '已生成两部门汇总' }], structuredContent: { total: 80 } }), null)
  expect(rows[0].text).toBe('本地表格 · 生成汇总')
  expect(rows[0].detail).toContain('已生成两部门汇总')
  expect(rows[0].detail).toContain('"total": 80')
})

it('MCP spilled failures keep service identity and full-result location', () => {
  const rows = actionWords(events({ ...metadata, schema: 'zhimeng.tool-result.spilled.v1', preview: '部分结果', spill_path: 'C:/state/result.json' }, true), null)
  expect(rows[0].text).toBe('本地表格 · 生成汇总')
  expect(rows[0].state).toBe('failed')
  expect(rows[0].detail).toContain('工具报告失败')
  expect(rows[0].detail).toContain('完整结果：C:/state/result.json')
})

it.each([false, true])('MCP write effects refresh actual project version even if failed=%s without a fake rollback card', (failed) => {
  const result = agentFileEffects(events({ ...metadata, result_revision: 5 }, failed), 'run')
  expect(result.writeAttempted).toBe(true)
  expect(result.refreshes).toHaveLength(1)
  expect(result.writes).toEqual([])
})

it('read-only MCP and lookalike results do not claim a file write', () => {
  expect(agentFileEffects(events({ ...metadata, read_only: true, result_revision: 5 }), 'run').refreshes).toEqual([])
  expect(agentFileEffects(events({ ...metadata, source_mcp: false, result_revision: 5 }), 'run').refreshes).toEqual([])
})

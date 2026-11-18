/**
 * 钉住 agentFileEffects：只有 Runtime 真实发出的「写文件成功」结果才算数，
 * Agent 嘴上说的「已保存」不算；停止/失败卡上的提示靠它区分「书没被动」和「已经存了几处」。
 */
import { describe, expect, it } from 'vitest'
import type { AgentActivity, AgentJsonValue } from '../../api/agent'
import { agentFileEffects } from '../lib/agentFileEffects'

let seq = 0
function activity(kind: string, payload: Record<string, AgentJsonValue>, runId = 'r1'): AgentActivity {
  seq += 1
  return {
    schema: 'zhimeng.activity-item.v1',
    activity_id: `a${seq}`,
    session_id: 's1',
    run_id: runId,
    sequence: seq,
    created_at: '2026-09-16T10:00:00Z',
    project_revision: null,
    correlation_id: 'c',
    causation_id: null,
    kind,
    payload,
  } as unknown as AgentActivity
}

function writeCall(id: string, runId?: string) {
  return activity('tool_call', { tool_call_id: id, tool_name: 'project_write_file', arguments: {} }, runId)
}

function writeResult(id: string, output: AgentJsonValue, status: 'succeeded' | 'failed' = 'succeeded', runId?: string) {
  return activity('tool_result', { tool_call_id: id, status, output, error: null, duration_ms: 10 }, runId)
}

const appliedOutput = {
  schema: 'zhimeng.tool-result.project-write-file.v1',
  applied: true,
  change_set_id: 'cs-1',
  relative_path: '08_素材库/灵感.md',
  result_revision: 5,
}

describe('agentFileEffects', () => {
  it.each([
    ['write_file', 'project-write-file'], ['edit_file', 'project-write-file'],
    ['move_file', 'project-move-file'], ['delete_file', 'project-delete-file'],
  ])('%s 的真实修改结果都刷新项目并保留修改单版本', (name, schema) => {
    const effects = agentFileEffects([
      activity('tool_call', { tool_call_id: 'mutate', tool_name: name, arguments: {} }),
      writeResult('mutate', { ...appliedOutput, schema: `zhimeng.tool-result.${schema}.v1`, result_revision: 6 }),
    ], 'r1')
    expect(effects.writes[0]).toMatchObject({ changeSetId: 'cs-1', revision: 6 })
    expect(effects.refreshes).toHaveLength(1)
    expect(effects.refreshes[0].revision).toBe(6)
  })

  it('建目录、文件日志跨边界修改和撤回即使没有change_set也刷新；失败或原本已有目录不冒充改动', () => {
    const effects = agentFileEffects([
      activity('tool_call', { tool_call_id: 'mkdir', tool_name: 'make_folder', arguments: {} }),
      writeResult('mkdir', { schema: 'zhimeng.tool-result.project-mkdir.v1', applied: true, result_revision: 5 }),
      writeResult('mkdir', { schema: 'zhimeng.tool-result.project-mkdir.v1', applied: false, result_revision: 5 }),
      activity('tool_call', { tool_call_id: 'move', tool_name: 'move_file', arguments: {} }),
      writeResult('move', { schema: 'zhimeng.tool-result.file-change.v1', change_id: 'fc-1', applied: true, result_revision: 6 }),
      writeResult('move', { schema: 'zhimeng.tool-result.file-change.v1', change_id: 'fc-fail', applied: true, result_revision: 6 }, 'failed'),
      activity('tool_call', { tool_call_id: 'undo', tool_name: 'undo_change', arguments: {} }),
      writeResult('undo', { schema: 'zhimeng.tool-result.project-undo-change.v1', change_id: 'fc-1', status: 'undone', result_revision: 7 }),
    ], 'r1')
    expect(effects.refreshes.map((effect) => effect.revision)).toEqual([5, 6, 7])
    expect(effects.writes).toEqual([])
  })

  it('命令已失败但Runtime带回更新后的项目版本仍需刷新，外部工具不能伪造命令结果', () => {
    const effects = agentFileEffects([
      activity('tool_call', { tool_call_id: 'cmd', tool_name: 'computer_run_command', arguments: {} }),
      writeResult('cmd', { schema: 'zhimeng.tool-result.computer-run-command.v1', exit_code: 1, result_revision: 8 }, 'failed'),
      writeCall('other'),
      writeResult('other', { schema: 'zhimeng.tool-result.computer-run-command.v1', exit_code: 0, result_revision: 9 }),
    ], 'r1')
    expect(effects.refreshes.map((effect) => effect.revision)).toEqual([8])
  })

  it('写文件成功且落盘的，收进 writes；失败或没落盘的不算', () => {
    const effects = agentFileEffects([
      writeCall('c1'), writeResult('c1', appliedOutput),
      writeCall('c2'), writeResult('c2', { ...appliedOutput, change_set_id: 'cs-2', applied: false }),
      writeCall('c3'), writeResult('c3', appliedOutput, 'failed'),
    ], null)
    expect(effects.writes).toHaveLength(1)
    expect(effects.writes[0]).toMatchObject({ changeSetId: 'cs-1', runId: 'r1', path: '08_素材库/灵感.md', revision: 5 })
    expect(effects.writeAttempted).toBe(true)
  })

  it('同一个修改单只记一次；只调用没结果的也算「试过写」', () => {
    const effects = agentFileEffects([
      writeCall('c1'), writeResult('c1', appliedOutput), writeResult('c1', appliedOutput),
      writeCall('c4'),
    ], null)
    expect(effects.writes).toHaveLength(1)
    expect(effects.writeAttempted).toBe(true)
    expect(effects.commandRan).toBe(false)
  })

  it('执行过命令（哪怕只是调用）commandRan 为真；别的工具结果冒充不了写入', () => {
    const effects = agentFileEffects([
      activity('tool_call', { tool_call_id: 'c9', tool_name: 'computer_run_command', arguments: {} }),
      activity('tool_result', { tool_call_id: 'c9', status: 'succeeded', output: { schema: 'zhimeng.tool-result.project-write-file.v1', applied: true, change_set_id: 'cs-x' }, error: null, duration_ms: 5 }),
    ], null)
    expect(effects.commandRan).toBe(true)
    expect(effects.writes).toEqual([])
    expect(effects.writeAttempted).toBe(false)
  })

  it('按 runId 过滤：别的轮次的写入不算进这一轮', () => {
    const effects = agentFileEffects([
      writeCall('c1', 'r1'), writeResult('c1', appliedOutput, 'succeeded', 'r1'),
      writeCall('c2', 'r2'), writeResult('c2', { ...appliedOutput, change_set_id: 'cs-2' }, 'succeeded', 'r2'),
    ], 'r2')
    expect(effects.writes.map((w) => w.changeSetId)).toEqual(['cs-2'])
  })

  it('不同轮次复用 tool_call_id 时按对应轮次的工具身份核对', () => {
    const effects = agentFileEffects([
      writeCall('same', 'r1'), writeResult('same', appliedOutput, 'succeeded', 'r1'),
      activity('tool_call', { tool_call_id: 'same', tool_name: 'computer_run_command', arguments: {} }, 'r2'),
      writeResult('same', { ...appliedOutput, change_set_id: 'cs-2' }, 'succeeded', 'r2'),
    ], null)
    expect(effects.writes.map((write) => write.changeSetId)).toEqual(['cs-1'])
  })
})

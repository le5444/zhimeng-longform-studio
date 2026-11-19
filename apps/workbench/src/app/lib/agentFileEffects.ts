import type { AgentActivity, AgentJsonValue } from '../../api/agent'
import { currentToolName } from './words'

function object(value: AgentJsonValue): Record<string, AgentJsonValue> | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value : null
}

const MUTATION_SCHEMAS: Record<string, readonly string[]> = {
  'zhimeng.tool-result.project-write-file.v1': ['write_file', 'edit_file'],
  'zhimeng.tool-result.project-move-file.v1': ['move_file'],
  'zhimeng.tool-result.project-delete-file.v1': ['delete_file'],
  'zhimeng.tool-result.project-mkdir.v1': ['make_folder'],
  'zhimeng.tool-result.file-change.v1': ['write_file', 'edit_file', 'move_file', 'delete_file', 'make_folder'],
  'zhimeng.tool-result.project-undo-change.v1': ['undo_change'],
  'zhimeng.tool-result.computer-run-command.v1': ['computer_run_command'],
  'zhimeng.tool-result.command-session.v1': ['computer_run_command', 'command_session'],
}
const WRITE_TOOLS = new Set(['write_file', 'edit_file', 'move_file', 'delete_file', 'make_folder', 'undo_change'])

/** 只认 Runtime 发出的写入结果，不能把 Agent 回答里的「已保存」当成文件事实。 */
export function agentFileEffects(activities: AgentActivity[], runId: string | null) {
  const items = activities.filter((item) => !runId || item.run_id === runId)
  const calls = new Map(items.flatMap((item) => item.kind === 'tool_call'
    ? [[`${item.run_id}:${item.payload.tool_call_id}`, currentToolName(item.payload.tool_name)] as const] : []))
  const writes = new Map<string, { changeSetId: string; runId: string; path: string; revision: number | null }>()
  // Agent 自己撤回的修改（undo_change，旧名 project_undo_change）：卡片要改成「已撤回」，书要重新读。
  const undone = new Map<string, { changeSetId: string; runId: string }>()
  const refreshes: { key: string; revision: number | null }[] = []
  let commandRan = items.some((item) => item.kind === 'command')
  let writeAttempted = false
  for (const name of calls.values()) {
    if (WRITE_TOOLS.has(name)) writeAttempted = true
    if (name === 'computer_run_command' || name === 'command_session') commandRan = true
  }
  for (const item of items) {
    if (item.kind !== 'tool_result') continue
    const result = object(item.payload.output)
    if (!result || typeof result.schema !== 'string') continue
    const toolName = calls.get(`${item.run_id}:${item.payload.tool_call_id}`)
    if (toolName?.startsWith('mcp__') && result.source_mcp === true) {
      // 外部工具的文件副作用没有本地撤回卡；Runtime检查过的版本用于刷新。
      if (result.read_only !== true) {
        writeAttempted = true
        if (typeof result.result_revision === 'number') refreshes.push({ key: `${item.run_id}:${item.activity_id ?? item.payload.tool_call_id}`, revision: result.result_revision })
      }
      continue
    }
    const allowed = MUTATION_SCHEMAS[result.schema]
    if (!allowed || (toolName && !allowed.includes(toolName))) continue
    const revision = typeof result.result_revision === 'number' ? result.result_revision : null
    const isUndo = result.schema === 'zhimeng.tool-result.project-undo-change.v1'
    const isCommand = result.schema === 'zhimeng.tool-result.computer-run-command.v1' || result.schema === 'zhimeng.tool-result.command-session.v1'
    const mutated = item.payload.status === 'succeeded' && (result.applied === true
      || (isUndo && (typeof result.change_set_id === 'string' || typeof result.change_id === 'string')))
    // 命令即使退出失败也可能已改文件；Runtime 返回的快照版本才是后续请求的依据。
    if (mutated || (isCommand && revision !== null)) {
      refreshes.push({ key: `${item.run_id}:${item.activity_id ?? item.payload.tool_call_id}`, revision })
    }
    if (isUndo) {
      if (item.payload.status === 'succeeded' && typeof result.change_set_id === 'string' && result.change_set_id.trim()) {
        undone.set(result.change_set_id, { changeSetId: result.change_set_id, runId: item.run_id })
      }
      continue
    }
    if (isCommand) continue
    writeAttempted = true
    if (item.payload.status !== 'succeeded' || result.applied !== true
      || typeof result.change_set_id !== 'string' || !result.change_set_id.trim()) continue
    writes.set(result.change_set_id, {
      changeSetId: result.change_set_id,
      runId: item.run_id,
      path: typeof result.relative_path === 'string' ? result.relative_path : '',
      revision,
    })
  }
  return { writes: [...writes.values()], undone: [...undone.values()], refreshes, commandRan, writeAttempted }
}

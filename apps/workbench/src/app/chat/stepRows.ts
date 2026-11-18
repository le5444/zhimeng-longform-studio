/**
 * 一轮里 Agent 做的事 → 界面上的一条条步骤（读了什么、改了什么、跑了什么命令、查了什么）。
 * 工具调用交给 actionWords 说人话（合并重复、试错不标红）；计划、技能、命令、产物、修改方案、报错在这里各成一条。
 * 每条带 kind，界面按它配图标。
 */
import type { AgentActivity } from '../../api/agent'
import type { ProjectInspection } from '../../api/projects'
import { authorFacingErrorText } from '../../lib/authorFacingError'
import { actionWords, pathDisplayName } from '../lib/words'
import { T } from '../strings'

export type StepState = 'running' | 'succeeded' | 'failed'
export type StepKind = 'read' | 'list' | 'search' | 'write' | 'command' | 'web' | 'skill' | 'memory' | 'plan' | 'change' | 'artifact' | 'error' | 'other'

export interface PlanItem {
  content: string
  status: 'pending' | 'in_progress' | 'completed'
}

export interface StepRow {
  key: string
  sequence?: number
  kind: StepKind
  text: string
  state: StepState
  outcome?: 'succeeded' | 'failed' | 'cancelled'
  commandSession?: { id: string; status: 'running' | 'exited' | 'cancelled' | 'interrupted'; exitCode: number | null }
  detail?: string
  sources?: import('../../api/sessionSource').SourceExcerpt[]
  durationMs?: number | null
  targetPath?: string
  fileResults?: import('./writeResults').WriteResult[]
  plan?: PlanItem[]
}

const TOOL_KINDS: Record<string, StepKind> = {
  read_file: 'read',
  project_read_file: 'read',
  skill_read_file: 'skill',
  list_files: 'list',
  project_list_files: 'list',
  skill_list_files: 'skill',
  project_search: 'search',
  search_web: 'web',
  web_search: 'web',
  web_fetch: 'web',
  shared_browser: 'web',
  write_file: 'write',
  edit_file: 'write',
  delete_file: 'write',
  move_file: 'write',
  make_folder: 'write',
  undo_change: 'write',
  change_set_propose: 'change',
  computer_run_command: 'command',
  command_session: 'command',
  memory_write: 'memory',
  session_search: 'search',
  session_read: 'read',
  plan_update: 'plan',
  plan_read: 'plan',
  ai_smell_score: 'other',
}

export function toolKind(tool: string | undefined): StepKind {
  return (tool && TOOL_KINDS[tool]) || 'other'
}

/** plan_update 的参数 → 清单条目；认不出的条目丢掉。 */
export function parsePlanItems(args: unknown): PlanItem[] {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return []
  const raw = (args as Record<string, unknown>).items
  if (!Array.isArray(raw)) return []
  const items: PlanItem[] = []
  for (const entry of raw) {
    if (!entry || typeof entry !== 'object' || Array.isArray(entry)) continue
    const content = String((entry as Record<string, unknown>).content ?? '').trim()
    if (!content) continue
    const statusRaw = (entry as Record<string, unknown>).status
    const status: PlanItem['status'] = statusRaw === 'in_progress' || statusRaw === 'completed' ? statusRaw : 'pending'
    items.push({ content, status })
    if (items.length >= 20) break
  }
  return items
}

const toolIdentity = (activity: Extract<AgentActivity, { kind: 'tool_call' | 'tool_result' }>) =>
  `${activity.run_id}:${activity.payload.tool_call_id}`

/** 成功结果才更新清单；失败和未确认的尝试保留为独立步骤。 */
function planRows(activities: AgentActivity[]): Array<StepRow & { sequence: number }> {
  const ordered = [...activities].sort((a, b) => a.sequence - b.sequence)
  const results = new Map(ordered.filter(item => item.kind === 'tool_result').map(item => [toolIdentity(item), item]))
  const accepted = new Map<string, StepRow & { sequence: number }>()
  const attempts: Array<StepRow & { sequence: number }> = []
  for (const call of ordered) {
    if (call.kind !== 'tool_call' || call.payload.tool_name !== 'plan_update') continue
    const key = toolIdentity(call)
    const result = results.get(key)
    const row: StepRow & { sequence: number } = {
      key: `plan-${key}`, sequence: call.sequence, kind: 'plan', text: '更新工作计划',
      state: !result ? 'running' : result.payload.status === 'succeeded' ? 'succeeded' : 'failed',
      outcome: result?.payload.status, durationMs: result?.payload.duration_ms,
      detail: result?.payload.error ? authorFacingErrorText(result.payload.error.error.message, '计划更新失败') : undefined,
    }
    if (result?.payload.status === 'succeeded') {
      const output = result.payload.output
      // 旧记录没有结果正文；只有已确认成功的空结果才回读参数。
      const legacy = output == null || (typeof output === 'object' && !Array.isArray(output) && Object.keys(output).length === 0)
      const items = parsePlanItems(legacy ? call.payload.arguments : output)
      if (items.length > 0) {
        accepted.set(call.run_id, { ...row, text: T.agent.events.plan, plan: items })
        continue
      }
      row.text = '计划结果无法读取'
      row.state = 'failed'
      row.outcome = undefined
      row.detail = textOf(output)
    }
    attempts.push(row)
  }
  return [...accepted.values(), ...attempts].sort((a, b) => a.sequence - b.sequence)
}

/** 这一轮最近一次成功记录的计划。 */
export function latestPlan(activities: AgentActivity[], runId: string | null): PlanItem[] | null {
  return planRows(runId ? activities.filter(item => item.run_id === runId) : activities).filter(row => row.plan).at(-1)?.plan ?? null
}

function textOf(value: unknown, limit = 1200): string {
  if (value === null || value === undefined) return ''
  const raw = typeof value === 'string' ? value : JSON.stringify(value, null, 2)
  return raw.length > limit ? `${raw.slice(0, limit)}…` : raw
}

/** 运行时每轮自己记的账（资料包、写作意图、核对回执）不是 Agent 做的事，不进步骤。 */
const BOOKKEEPING_ARTIFACTS = new Set(['context_pack', 'writer_intent', 'context_injection_receipt'])

/** 完整处理过程不裁掉早期工具；显式 limit 仅用于失败卡等简短预览。 */
export function stepRows(activities: AgentActivity[], snapshot: ProjectInspection | null, limit = Infinity): StepRow[] {
  const E = T.agent.events
  const rows: Array<StepRow & { sequence: number }> = planRows(activities)
  const planCalls = activities.filter(
    (item): item is Extract<AgentActivity, { kind: 'tool_call' }> => item.kind === 'tool_call' && item.payload.tool_name === 'plan_update',
  )
  const planIds = new Set(planCalls.map(toolIdentity))
  const toolish = activities.filter((item) => (item.kind === 'tool_call' || item.kind === 'tool_result') && !planIds.has(toolIdentity(item)))
  const results = new Map(toolish.filter((item) => item.kind === 'tool_result').map((item) => [item.payload.tool_call_id, item]))
  const callGroups: AgentActivity[][] = []
  let group: AgentActivity[] = []
  for (const item of [...activities].sort((a, b) => a.sequence - b.sequence)) {
    if (item.kind === 'tool_call' && !planIds.has(toolIdentity(item))) group.push(item)
    else if ((item.kind === 'reasoning' && !isParameterNote(item)) || (item.kind === 'message' && item.payload.role === 'assistant')) {
      if (group.length > 0) callGroups.push(group)
      group = []
    }
  }
  if (group.length > 0) callGroups.push(group)
  const toolSteps = callGroups.flatMap((calls) => actionWords([
    ...calls,
    ...calls.flatMap((item) => {
      const result = item.kind === 'tool_call' ? results.get(item.payload.tool_call_id) : undefined
      return result ? [result] : []
    }),
  ], snapshot, Infinity)).slice(-limit)
  for (const step of toolSteps) {
    rows.push({
      key: `tool-${step.key}`,
      kind: toolKind(step.tool),
      text: step.text,
      state: step.state,
      outcome: results.get(step.key)?.payload.status,
      durationMs: step.durationMs,
      detail: step.detail,
      sources: step.sources,
      targetPath: step.targetPath,
      fileResults: step.fileResults,
      commandSession: step.commandSession,
      sequence: step.sequence,
    })
  }
  for (const activity of activities) {
    if (activity.kind === 'skill_call') {
      const payload = activity.payload
      rows.push({
        key: `skill-${payload.skill_call_id}`,
        kind: 'skill',
        text: E.skill(payload.skill_id),
        state: payload.status === 'succeeded' ? 'succeeded' : payload.status === 'failed' || payload.status === 'cancelled' ? 'failed' : 'running',
        detail: textOf(payload.output),
        sequence: activity.sequence,
      })
    } else if (activity.kind === 'command') {
      const payload = activity.payload
      const state: StepState = payload.status === 'succeeded' ? 'succeeded' : payload.status === 'failed' || payload.status === 'cancelled' ? 'failed' : 'running'
      rows.push({
        key: `command-${payload.command_id}`,
        kind: 'command',
        text: state === 'running' ? E.commandRunning : '运行命令',
        state,
        detail: `${payload.command}${payload.exit_code != null ? `\n[退出代码: ${payload.exit_code}]` : ''}`,
        sequence: activity.sequence,
      })
    } else if (activity.kind === 'artifact') {
      const payload = activity.payload
      if (BOOKKEEPING_ARTIFACTS.has(payload.artifact_type)) continue
      const name = payload.name || payload.artifact_type
      rows.push({
        key: `artifact-${payload.artifact_id}`,
        kind: 'artifact',
        text: E.artifact(/[一-鿿]/.test(name) ? name : E.artifactFallback),
        state: 'succeeded',
        sequence: activity.sequence,
      })
    } else if (activity.kind === 'change_proposal') {
      const payload = activity.payload
      const files = payload.files.map((file) => pathDisplayName(snapshot, file.path))
      rows.push({
        key: `proposal-${payload.proposal_id}`,
        kind: 'change',
        text: E.proposal(files.length === 1 ? files[0] : E.files(files.length)),
        state: 'succeeded',
        detail: payload.summary,
        targetPath: payload.files.length === 1 ? payload.files[0].path : undefined,
        sequence: activity.sequence,
      })
    } else if (activity.kind === 'error') {
      rows.push({
        key: `error-${activity.activity_id}`,
        kind: 'error',
        text: '执行记录',
        detail: authorFacingErrorText(activity.payload.error.error.message, E.errorFallback),
        state: 'failed',
        sequence: activity.sequence,
      })
    }
  }
  return rows.sort((a, b) => a.sequence - b.sequence)
}

/** 请求参数说明（「本次按 xx 档发送……」）不是模型的思考，单独摆。 */
export function isParameterNote(activity: AgentActivity): boolean {
  return activity.kind === 'reasoning' && (activity.payload.source === 'runtime'
    || (activity.payload.summary.startsWith('本次')
      && (activity.payload.content ?? '').includes('这描述的是发送参数')))
}

export interface ThoughtBlock {
  key: string
  text: string
  sequence: number
  phase: 'delta' | 'completed' | 'interrupted'
  visibility: 'summary' | 'full' | 'redacted'
}

/** 同一块增量合起来，终态快照校正正文；重连重放同一活动不会重复添加。 */
export function thoughtBlocksOf(activities: AgentActivity[], runId: string | null): ThoughtBlock[] {
  const blocks = new Map<string, ThoughtBlock>()
  const seenActivities = new Set<string>()
  const legacyTexts = new Set<string>()
  for (const activity of [...activities].sort((a, b) => a.sequence - b.sequence)) {
    if (activity.kind !== 'reasoning' || isParameterNote(activity)) continue
    if (runId && activity.run_id !== runId) continue
    const identity = `${activity.run_id}:${activity.activity_id ?? activity.sequence}`
    if (seenActivities.has(identity)) continue
    seenActivities.add(identity)
    const payload = activity.payload
    const key = payload.block_id ? `${activity.run_id}:${payload.block_id}` : identity
    const existing = blocks.get(key)
    const phase = payload.phase ?? 'completed'
    const text = phase === 'delta'
      ? (existing?.text ?? '') + (payload.delta ?? payload.content ?? '')
      : payload.content ?? existing?.text ?? payload.summary ?? ''
    if (!payload.block_id) {
      const legacyText = text.trim()
      if (!legacyText || legacyTexts.has(`${activity.run_id}:${legacyText}`)) continue
      legacyTexts.add(`${activity.run_id}:${legacyText}`)
    }
    blocks.set(key, {
      key, text, sequence: existing?.sequence ?? activity.sequence,
      phase, visibility: payload.visibility ?? 'full',
    })
  }
  return [...blocks.values()].filter((block) => block.text.trim().length > 0)
}

/** 一轮里模型可展示的思考（按先后），不把每个 token 当成一段。 */
export function thoughtsOf(activities: AgentActivity[], runId: string | null): string[] {
  return thoughtBlocksOf(activities, runId).map((block) => block.text.trim())
}

/** 最新一次已保存回复之后的过程，供进行中和中断后的尾段共用。 */
export function turnTailActivities(activities: AgentActivity[], runId: string | null): AgentActivity[] {
  const run = activities.filter((item) => !runId || item.run_id === runId)
  const completed = run.reduce((sequence, item) => item.kind === 'message'
    && item.payload.role === 'assistant' && item.payload.phase === 'completed' ? Math.max(sequence, item.sequence) : sequence, 0)
  return run.filter((item) => item.sequence > completed)
}

/** 最新一段思考的第一行（截到 40 字）：状态行里滚动的那句。 */
export function latestThoughtLine(activities: AgentActivity[], runId: string | null): string {
  const thoughts = thoughtsOf(activities, runId)
  const last = thoughts.at(-1) ?? ''
  const line = last.split('\n').map((item) => item.trim()).find(Boolean) ?? ''
  return line.length > 40 ? `${line.slice(0, 40)}…` : line
}

export type StepItem =
  | { type: 'row'; row: StepRow }
  | { type: 'group'; key: string; group: 'explore' | 'edit'; rows: StepRow[]; state: StepState }

const EXPLORE_KINDS = new Set<StepKind>(['read', 'list', 'search'])

/**
 * 连着的读 / 列目录 / 搜索并成一组「已探索」，连着的改文件并成一组「改了 N 个文件」（前端吸收 9-29：OpenCode ContextToolGroup、
 * ZCode explore / changes-group）。一组至少两步才并；有一步在跑整组算在跑，有一步没成整组算没成。
 */
export function groupSteps(rows: StepRow[]): StepItem[] {
  const items: StepItem[] = []
  let run = null as { group: 'explore' | 'edit'; rows: StepRow[] } | null
  const flush = () => {
    if (!run) return
    if (run.rows.length >= 2) {
      const state: StepState = run.rows.some((row) => row.state === 'running') ? 'running' : run.rows.some((row) => row.state === 'failed') ? 'failed' : 'succeeded'
      items.push({ type: 'group', key: `${run.group}-${run.rows[0].key}`, group: run.group, rows: run.rows, state })
    } else {
      items.push({ type: 'row', row: run.rows[0] })
    }
    run = null
  }
  for (const row of rows) {
    const group = EXPLORE_KINDS.has(row.kind) ? 'explore' : row.kind === 'write' ? 'edit' : null
    if (group && run?.group === group) {
      run.rows.push(row)
      continue
    }
    flush()
    if (group) run = { group, rows: [row] }
    else items.push({ type: 'row', row })
  }
  flush()
  return items
}

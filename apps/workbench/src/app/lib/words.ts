/**
 * 把机器里的东西翻成带对象的整句：Agent 在做什么、一次修改现在怎样了。
 * 界面上所有「说人话」的转换都集中在这里，改措辞只改这一处。
 */
import type { AgentActivity } from '../../api/agent'
import type { ProjectInspection } from '../../api/projects'
import { T } from '../strings'
import { writeResults, type WriteResult } from '../chat/writeResults'

/**
 * 一个文件路径在界面上怎么叫：文件名，带扩展名（和步骤里「读取 xx.md」一个叫法）。
 * 9-29 起不再把文件翻成「第 N 章 名字」：和 Codex、Claude Code 一样，它是什么由文件自己说。
 */
export function pathDisplayName(_snapshot: ProjectInspection | null, path: string): string {
  const normalized = path.replace(/\\/g, '/').replace(/\/+$/, '')
  return normalized.split('/').pop() || normalized
}

/** 时间说人话：今天 09:41 / 昨天 18:02 / 9月11日 08:39 / 2025年3月2日。 */
export function whenText(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (n: number) => String(n).padStart(2, '0')
  const hm = `${pad(date.getHours())}:${pad(date.getMinutes())}`
  const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(date, now)) return `今天 ${hm}`
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return `昨天 ${hm}`
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日 ${hm}`
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

/** 左栏对话行右端的小字日期：今天 / 昨天 / N 天前 / 9月14日 / 2025年9月14日。 */
export function shortDay(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(date, now)) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return '昨天'
  const days = Math.floor((now.getTime() - date.getTime()) / 86400000)
  if (days > 1 && days < 7) return `${days} 天前`
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

const WEB_SEARCH_SCHEMA = 'zhimeng.tool-result.web-search.v1'

/** 联网搜索那一步：说搜了什么、几条；展开是搜索服务、时间和每条的标题、日期、网址。 */
function webSearchWords(query: string, out: Record<string, unknown> | null, done: boolean): { text: string; detail: string | undefined } {
  const results = Array.isArray(out?.results) ? out.results : []
  const text = done ? T.agent.act.searchWeb(query, results.length) : T.agent.act.searchingWeb(query)
  if (!out) return { text, detail: undefined }
  const rows = results.flatMap((item) => {
    if (!item || typeof item !== 'object') return []
    const record = item as Record<string, unknown>
    const url = typeof record.url === 'string' ? record.url : ''
    if (!url) return []
    const title = typeof record.title === 'string' && record.title ? record.title : url
    const date = typeof record.published === 'string' && record.published ? `（${record.published.slice(0, 10)}）` : ''
    return [`${title}${date}\n${url}`]
  })
  const source = T.agent.act.searchSource(
    typeof out.provider === 'string' ? out.provider : '',
    typeof out.searched_at === 'string' ? out.searched_at.replace('T', ' ').slice(0, 16) : '',
  )
  return { text, detail: [source, ...(rows.length > 0 ? rows : [T.agent.act.searchNothing])].join('\n\n') }
}

/**
 * 写文件那组工具 9-26 改了中性名字，读和列 9-27 合成 list_files / read_file 一套（纠偏 3.11）；
 * 旧对话里记的是旧名字，显示时一律按新名字认（project_list_files / project_read_file 参数不一样，照旧单独认）。
 */
const LEGACY_TOOL_NAMES: Record<string, string> = {
  computer_list_files: 'list_files',
  computer_read_file: 'read_file',
  project_write_file: 'write_file',
  project_edit_file: 'edit_file',
  project_delete_file: 'delete_file',
  project_move_file: 'move_file',
  project_mkdir: 'make_folder',
  project_undo_change: 'undo_change',
}

export function currentToolName(name: string): string {
  return LEGACY_TOOL_NAMES[name] ?? name
}

function argString(args: unknown, keys: string[]): string | null {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return null
  const record = args as Record<string, unknown>
  for (const key of keys) {
    const value = record[key]
    if (typeof value === 'string' && value.trim()) return value.trim()
  }
  return null
}

/**
 * 试错里正常的一步：没找到、没对上、已经有了、参数没填对……照实写出结果，不标红、不算「失败」。
 * 只有断网、没权限、程序出错、超时这类真失败才标红（纠偏指令 3.4；9-26 真机：模型找记忆文件
 * 试了几个地方，界面满屏「（失败）」，看着像坏了）。键是后台工具报错的 code。
 */
const TRIAL_OUTCOMES: Record<string, string> = {
  agent_tool_computer_path_not_found: '没找到',
  agent_tool_path_not_found: '没找到',
  agent_tool_edit_target_missing: '没找到这个文件',
  agent_tool_delete_target_missing: '没找到这个文件',
  agent_tool_move_source_missing: '没找到这个文件',
  agent_tool_computer_not_a_folder: '这是文件，不是文件夹',
  agent_tool_computer_file_not_text: '不是纯文字，要先转换',
  agent_tool_file_not_text: '不是纯文字',
  agent_tool_computer_path_invalid: '这个路径打不开',
  agent_tool_edit_find_missing: '没找到要改的那段',
  agent_tool_edit_find_ambiguous: '要改的那段出现了好几处',
  agent_tool_edit_noop: '没有要改的地方',
  agent_tool_move_target_exists: '目标位置已经有文件',
  agent_tool_move_same_path: '位置没变',
  agent_tool_mkdir_target_exists: '那里已经有同名的东西',
  agent_tool_undo_nothing: '没有可撤回的修改',
  agent_tool_undo_not_applied: '已经撤回过了',
  agent_tool_undo_target_missing: '没有这条修改',
  agent_tool_validation_failed: '参数没填对',
  agent_tool_arguments_invalid: '参数没填对',
  agent_tool_web_url_invalid: '网址写法不对',
  agent_tool_web_not_text: '不是网页文字',
}

export interface ActionWords {
  key: string
  text: string
  state: 'running' | 'succeeded' | 'failed'
  sequence: number
  /** 这一步从发起到出结果花了多少毫秒；还在跑（没有结果）就是 null。 */
  durationMs: number | null
  detail?: string
  sources?: import('../../api/sessionSource').SourceExcerpt[]
  /** 这一步针对的文件路径，供中栏跳转打开 */
  targetPath?: string
  fileResults?: WriteResult[]
  /** 调的是哪个工具（旧名字已经换成现在的名字）：新界面按它给每一步配图标。 */
  tool?: string
  commandSession?: { id: string; status: 'running' | 'exited' | 'cancelled' | 'interrupted'; exitCode: number | null }
}

/**
 * Agent 的工具调用 → 正常 Agent 语言的一行：动词 + 真实文件名。
 * 「读取 004_玉佩上的青云.md」「搜索「柳青烟」」。
 */
/** 文件在事件行里就叫自己的文件名（带扩展名），不翻成「第 N 章 名字」。 */
function baseName(path: string): string {
  return path.replace(/\\/g, '/').split('/').pop() ?? path
}

export function actionWords(activities: AgentActivity[], snapshot: ProjectInspection | null, limit = 8): ActionWords[] {
  const results = new Map<string, Extract<AgentActivity, { kind: 'tool_result' }>>()
  for (const activity of activities) {
    if (activity.kind === 'tool_result') results.set(activity.payload.tool_call_id, activity)
  }
  const items: ActionWords[] = []
  for (const activity of activities) {
    if (activity.kind !== 'tool_call') continue
    const result = results.get(activity.payload.tool_call_id)
    const state: ActionWords['state'] = !result
      ? 'running'
      : result.payload.status === 'succeeded' ? 'succeeded' : 'failed'
    const done = state !== 'running'
    const name = currentToolName(activity.payload.tool_name)
    const args = activity.payload.arguments
    let text: string
    let detail: string | undefined
    let sources: ActionWords['sources']
    let targetPath: string | undefined
    let fileResults: WriteResult[] | undefined
    if (name === 'project_list_files') {
      text = T.agent.act.listFiles
    } else if (name === 'project_read_file') {
      const path = argString(args, ['relative_path', 'path', 'file_path'])
      const fileId = argString(args, ['file_id'])
      let display = path ? baseName(path) : ''
      if (!display && fileId && snapshot) {
        const file = snapshot.files.find((item) => item.file_id === fileId)
        if (file) display = baseName(file.relative_path)
      }
      targetPath = path || (fileId && snapshot ? snapshot.files.find((item) => item.file_id === fileId)?.relative_path : undefined)
      if (!display) display = '一份文件'
      text = done ? T.agent.act.readFile(display) : T.agent.act.reading(display)
    } else if (name === 'project_search') {
      const query = argString(args, ['query', 'q', 'keyword']) ?? ''
      text = done ? T.agent.act.search(query) : T.agent.act.searching(query)
      if (result) {
        const out = result.payload.output as Record<string, unknown> | null
        if (out && typeof out === 'object') {
          const matches = Array.isArray(out.matches) ? out.matches : Array.isArray(out.results) ? out.results : null
          if (matches && matches.length > 0) {
            detail = matches.slice(0, 10).map((m: Record<string, unknown>) => {
              const p = String(m.relative_path ?? m.path ?? m.title ?? '')
              const snippet = String(m.snippet ?? m.excerpt ?? m.content ?? '')
              return p ? `${baseName(p)}:\n${snippet}` : snippet
            }).filter(Boolean).join('\n---\n')
          }
        }
      }
    } else if (name === 'skill_list_files') {
      text = T.agent.act.skillList
    } else if (name === 'skill_read_file') {
      const path = argString(args, ['path', 'relative_path']) ?? ''
      targetPath = path || undefined
      text = T.agent.act.skillRead(path.split(/[\\/]/).pop()?.replace(/\.(md|txt|jsonl)$/i, '') ?? path)
    } else if (name === 'skill_search') {
      text = T.agent.act.skillSearch(argString(args, ['query', 'q', 'keyword']) ?? '')
    } else if (name === 'change_set_propose') {
      text = done ? T.agent.act.propose : T.agent.act.proposing
    } else if (name === 'write_file' || name === 'edit_file') {
      const list = args && typeof args === 'object' && !Array.isArray(args)
        ? (args as Record<string, unknown>).files
        : null
      const files = Array.isArray(list) ? list : null
      // 写到打开的文件夹里时，结果里有文件夹内的相对路径（给的是绝对路径也换算好了），跳转按它。
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const writtenRelative = typeof out?.relative_path === 'string' ? out.relative_path : null
      if (name === 'write_file' && files) {
        // 一次写多份：一句话说份数，跳转指到第一份。
        const first = files[0] && typeof files[0] === 'object'
          ? argString(files[0], ['path', 'relative_path'])
          : null
        targetPath = first ?? undefined
        text = done ? T.agent.act.writeFiles(files.length) : T.agent.act.writingFiles(files.length)
      } else {
        const path = argString(args, ['path', 'relative_path']) ?? ''
        targetPath = (writtenRelative ?? path) || undefined
        const display = path ? baseName(path) : '一个文件'
        text = name === 'write_file'
          ? (done ? T.agent.act.writeFile(display) : T.agent.act.writingFile(display))
          : (done ? T.agent.act.editFile(display) : T.agent.act.editingFile(display))
      }
      if (result && files) {
        fileResults = writeResults(out)
        if (fileResults.length) {
          targetPath = fileResults.find(file => file.status === 'saved' || file.status === 'unchanged')?.path
          const saved = fileResults.filter(file => file.status === 'saved').length
          const unknown = fileResults.filter(file => file.status === 'unconfirmed').length
          text = out?.incomplete === true || unknown > 0 ? `保存未完成 · 已保存 ${saved} 个文件`
            : fileResults.every(file => file.status === 'unchanged') ? '文件内容未变更'
              : fileResults.every(file => file.status === 'rolled_back') ? '文件修改已撤回'
                : `已保存 ${saved} 个文件`
          detail = typeof out?.message === 'string' && !result.payload.error?.error.message.startsWith(out.message) ? out.message : undefined
        } else if (state === 'failed') {
          text = result.payload.status === 'cancelled' ? '保存已停止 · 结果未确认' : '保存未完成'
          targetPath = undefined
        }
      }
    } else if (name === 'delete_file') {
      const path = argString(args, ['path', 'relative_path']) ?? ''
      targetPath = path || undefined
      const display = path ? baseName(path) : '一个文件'
      text = done ? T.agent.act.deleteFile(display) : T.agent.act.deletingFile(display)
    } else if (name === 'move_file') {
      const from = argString(args, ['path', 'relative_path']) ?? ''
      const to = argString(args, ['target_path', 'target_relative_path']) ?? ''
      targetPath = (to || from) || undefined
      const displayFrom = from ? baseName(from) : '一个文件'
      const displayTo = to ? baseName(to) : '新位置'
      text = done
        ? T.agent.act.moveFile(displayFrom, displayTo)
        : T.agent.act.movingFile(displayFrom, displayTo)
    } else if (name === 'make_folder') {
      const path = argString(args, ['path', 'relative_path']) ?? ''
      text = done ? T.agent.act.makeDir(path) : T.agent.act.makingDir(path)
    } else if (name === 'undo_change') {
      // 撤回的是哪份文件，只有结果里才有；进行中只说在撤回。
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const files = out && typeof out === 'object' && Array.isArray(out.files) ? out.files : []
      const first = files[0] && typeof files[0] === 'object'
        ? argString(files[0], ['relative_path', 'path'])
        : null
      const path = first ?? ''
      targetPath = path || undefined
      text = done ? T.agent.act.undoChange(path ? baseName(path) : '') : T.agent.act.undoingChange
    } else if (name === 'shared_browser') {
      const action = argString(args, ['action']) ?? ''
      const labels: Record<string, string> = { list: '查看网页与下载', open: '打开共享网页', observe: '查看当前网页', click: '点击网页', fill: '填写网页', press: '在网页按键', scroll: '滚动网页', navigate: '打开网址', back: '网页后退', forward: '网页前进', reload: '刷新网页', screenshot: '保存网页截图', close: '关闭网页', receipt: '核对浏览器操作记录' }
      text = labels[action] ?? '操作共享浏览器'
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const tab = out?.tab && typeof out.tab === 'object' ? out.tab as Record<string, unknown> : out
      const lines = [typeof tab?.title === 'string' ? tab.title : '', typeof tab?.url === 'string' ? tab.url : argString(args, ['url']) ?? '']
      const page = out?.page && typeof out.page === 'object' ? out.page as Record<string, unknown> : null
      if (typeof page?.text === 'string') lines.push(page.text.length > 1200 ? `${page.text.slice(0, 1200)}…` : page.text)
      if (typeof out?.path === 'string') lines.push(out.path)
      if (out?.receipt_recovered === true) lines.push('已从持久记录找回原操作结果，没有重复执行。')
      detail = lines.filter(Boolean).join('\n\n') || undefined
    } else if (name === 'web_fetch' && (result?.payload.output as Record<string, unknown> | null | undefined)?.schema === WEB_SEARCH_SCHEMA) {
      // 模型拿 web_fetch 开搜索引擎的结果页（或传了一句搜索词），后台改用联网搜索了：按搜索显示。
      const out = result?.payload.output as Record<string, unknown>
      const query = typeof out.query === 'string' ? out.query : argString(args, ['url']) ?? ''
      ;({ text, detail } = webSearchWords(query, out, done))
    } else if (name === 'web_fetch') {
      const url = argString(args, ['url']) ?? ''
      let host = url
      try {
        host = new URL(url).hostname || url
      } catch {
        // URL 不是合法写法时原样显示
      }
      text = done ? T.agent.act.fetchPage(host) : T.agent.act.fetchingPage(host)
      // 展开看得到完整网址、标题和抓回来的开头：查了什么一眼能核。
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const lines: string[] = [typeof out?.final_url === 'string' && out.final_url ? out.final_url : url]
      if (typeof out?.title === 'string' && out.title) lines.push(out.title)
      if (typeof out?.content === 'string' && out.content) {
        lines.push(out.content.length > 600 ? `${out.content.slice(0, 600)}…` : out.content)
      }
      if (url) detail = lines.join('\n\n')
    } else if (name === 'search_web' || name === 'web_search') {
      const query = argString(args, ['query']) ?? ''
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      ;({ text, detail } = webSearchWords(query, out ?? null, done))
    } else if (name === 'ai_smell_score') {
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const score = typeof out?.score === 'number' ? out.score : null
      const level = typeof out?.level === 'string' ? out.level : ''
      text = done && score !== null ? T.agent.act.aiSmellScored(Math.round(score), level) : T.agent.act.aiSmellScoring
      if (typeof out?.note === 'string' && out.note) detail = out.note
    } else if (name === 'session_search' || name === 'session_read') {
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      const query = argString(args, ['query']) ?? ''
      const entries = out?.[name === 'session_search' ? 'matches' : 'messages']
      const messages = Array.isArray(entries) ? entries.filter((item): item is Record<string, unknown> => !!item && typeof item === 'object' && !Array.isArray(item)) : []
      text = name === 'session_search'
        ? done ? `找到 ${messages.length} 条历史消息` : query ? `搜索旧对话：${query}` : '查看最近对话'
        : done ? `读取了 ${messages.length} 条历史消息` : '读取旧对话'
      const scope = out?.scope === 'all' ? '全部对话' : out?.scope === 'project' ? (out.project_id ? '当前项目' : '自由对话') : ''
      sources = messages.map((message) => {
        const title = typeof message.session_title === 'string' ? message.session_title : '旧对话'
        const speaker = message.role === 'user' ? '用户' : 'Agent'
        const date = typeof message.created_at === 'string' ? shortDay(message.created_at) : ''
        const project = typeof message.project_name === 'string' ? message.project_name : '自由对话'
        const background = message.origin_kind === 'command_completion' ? '后台命令接续' : ''
        const ordinal = typeof message.ordinal === 'number' ? `第 ${message.ordinal} 条` : ''
        const partial = (typeof message.start_offset === 'number' && message.start_offset > 0) || typeof message.next_start_offset === 'number' ? '部分原文' : ''
        const source = [title, project, speaker, date, ordinal, background, partial].filter(Boolean).join(' · ')
        return { sessionId: typeof message.session_id === 'string' ? message.session_id : '',
          messageId: typeof message.message_id === 'string' ? message.message_id : '', label: source,
          content: typeof message.content === 'string' ? message.content : '' }
      })
      detail = [query, scope, out?.has_more === true ? '还有更多历史消息' : ''].filter(Boolean).join('\n\n') || undefined
    } else if (name === 'memory_write') {
      const remembered = argString(args, ['text']) ?? ''
      const preview = remembered.length > 60 ? `${remembered.slice(0, 60)}…` : remembered
      const action = argString(args, ['action']) ?? 'add'
      const old = argString(args, ['old_text'])
      const out = result?.payload.output as Record<string, unknown> | null | undefined
      text = done && out?.applied === false ? '记忆未变更' : action === 'replace' ? (done ? '已更新记忆' : '更新记忆')
        : action === 'remove' ? (done ? '已移除记忆' : '移除记忆') : done ? T.agent.act.remember(preview) : T.agent.act.remembering(preview)
      detail = action === 'add' ? (remembered.length > 60 ? remembered : undefined)
        : [old ? `原来：${old}` : '', remembered ? `现在：${remembered}` : ''].filter(Boolean).join('\n') || undefined
    } else if (name === 'plan_update') {
      text = done ? T.agent.act.planUpdate : T.agent.act.planUpdating
    } else if (name === 'plan_read') {
      text = done ? '已读取工作计划' : '读取工作计划'
    } else if (name === 'list_files') {
      const path = argString(args, ['path']) ?? ''
      text = path ? T.agent.act.listFolder(baseName(path.replace(/[\\/]+$/, '')) || path) : T.agent.act.listFiles
    } else if (name === 'read_file') {
      const path = argString(args, ['path', 'relative_path', 'file_path']) ?? ''
      targetPath = path || undefined
      const display = path ? baseName(path) : '一份文件'
      text = done ? T.agent.act.readFile(display) : T.agent.act.reading(display)
    } else if (name === 'computer_run_command' || name === 'command_session') {
      const command = (argString(args, ['command']) ?? '').trim()
      const firstLine = command.split('\n')[0].trim()
      const preview = firstLine.length > 40 ? `${firstLine.slice(0, 40)}…` : firstLine
      text = done ? T.agent.act.runCommand(preview) : T.agent.act.runningCommand(preview)
      if (name === 'command_session') {
        const action = argString(args, ['action'])
        text = action === 'input' ? '向命令输入内容' : action === 'stop' ? '停止命令' : action === 'list' ? '查看命令会话' : '读取命令输出'
      }
      if (result) {
        const out = result.payload.output as Record<string, unknown> | null
        if (out && typeof out === 'object') {
          const stdout = typeof out.stdout === 'string' ? out.stdout : ''
          const stderr = typeof out.stderr === 'string' ? out.stderr : ''
          const exitCode = typeof out.exit_code === 'number' ? out.exit_code : null
          const blocks: string[] = []
          const originalCommand = command || (typeof out.command === 'string' ? out.command : '')
          if (originalCommand) blocks.push(`$ ${originalCommand}`)
          if (typeof out.session_id === 'string') blocks.push(`[命令会话: ${out.session_id}]`)
          if (out.status === 'running') blocks.push('本次读取时命令仍在运行，可继续读取输出或发送输入。')
          if (out.status === 'interrupted') blocks.push('此命令会话已中断，未重新执行。')
          if (exitCode != null) blocks.push(`[退出代码: ${exitCode}]`)
          if (typeof out.output_error === 'string' && out.output_error) {
            text += ' · 输出不完整'
            blocks.push(`[输出不完整]\n${out.output_error}`)
          }
          if (stdout.trim()) blocks.push(stdout.trim())
          if (stderr.trim()) blocks.push(`[错误输出]\n${stderr.trim()}`)
          if (Array.isArray(out.sessions)) blocks.push(...out.sessions.map((session) => {
            const item = session as Record<string, unknown>
            return `${item.session_id ?? ''} · ${item.status ?? ''}${typeof item.command === 'string' ? `\n$ ${item.command}` : ''}`
          }))
          if (typeof out.stdout_file === 'string') blocks.push(`完整输出：${out.stdout_file}`)
          if (typeof out.stderr_file === 'string') blocks.push(`错误输出：${out.stderr_file}`)
          detail = blocks.join('\n\n')
        }
      } else if (command) {
        detail = `$ ${command}`
      }
    } else if (name.startsWith('mcp__')) {
      const out = result?.payload.output as Record<string, unknown> | null
      const rawName = name.slice(5).replace(/_[a-f0-9]{32}$/, '')
      const tool = out && typeof out.title === 'string' && out.title.trim() ? out.title
        : out && typeof out.tool === 'string' ? out.tool : rawName
      const server = out && typeof out.server === 'string' ? out.server : 'MCP'
      text = `${server} · ${tool}`
      if (out && typeof out === 'object') {
        const blocks: string[] = []
        if (Array.isArray(out.content)) for (const block of out.content) {
          if (block && typeof block === 'object' && typeof (block as Record<string, unknown>).text === 'string') blocks.push((block as { text: string }).text)
        }
        if (out.structuredContent != null) blocks.push(`[结构化结果]\n${JSON.stringify(out.structuredContent, null, 2)}`)
        if (typeof out.preview === 'string') blocks.push(out.preview)
        if (typeof out.spill_path === 'string') blocks.push(`完整结果：${out.spill_path}`)
        if (typeof out.content_note === 'string') blocks.push(out.content_note)
        detail = blocks.join('\n\n') || undefined
      }
    } else if (name === 'candidate_output_submit') {
      text = T.agent.act.submit
    } else if (name === 'skill_execute') {
      text = T.agent.act.check
    } else {
      text = T.agent.act.generic
    }
    const errorCode = result?.payload.error?.error?.code
    const trialOutcome = state === 'failed' && errorCode ? TRIAL_OUTCOMES[errorCode] : undefined
    if (result?.payload.error?.error?.message) {
      const err = result.payload.error.error.message
      // 试错的那一步，展开看得到工具的原话（「没有找到这个文件：…，那个文件夹里有：…」），不贴「执行失败」。
      const line = trialOutcome ? err : `[执行失败]: ${err}`
      detail = detail ? `${detail}\n\n${line}` : line
    }
    // 摘要只显示做了什么；失败原因保留在展开详情，整轮恢复由任务状态负责。
    const startedAt = Date.parse(activity.created_at)
    const endedAt = result ? Date.parse(result.created_at) : Number.NaN
    const durationMs = result && Number.isFinite(startedAt) && Number.isFinite(endedAt)
      ? Math.max(0, endedAt - startedAt)
      : null
    items.push({
      key: activity.payload.tool_call_id,
      text,
      state: trialOutcome ? 'succeeded' : state,
      sequence: activity.sequence,
      durationMs,
      detail,
      sources,
      targetPath,
      fileResults,
      tool: name,
      ...commandSessionResult(name, result?.payload.output),
    })
  }
  return mergeRepeats(items.sort((a, b) => a.sequence - b.sequence)).slice(-limit)
}

function commandSessionResult(tool: string, output: unknown): Pick<ActionWords, 'commandSession'> {
  if (!['computer_run_command', 'command_session'].includes(tool) || !output || typeof output !== 'object') return {}
  const item = output as Record<string, unknown>
  if (item.schema !== 'zhimeng.tool-result.command-session.v1' || typeof item.session_id !== 'string'
    || !['running', 'exited', 'cancelled', 'interrupted'].includes(String(item.status))) return {}
  return { commandSession: { id: item.session_id, status: item.status as NonNullable<ActionWords['commandSession']>['status'], exitCode: typeof item.exit_code === 'number' ? item.exit_code : null } }
}

/** 连着几条一样的（Agent 把同一个文件读了三遍）合成一条，后面标次数；耗时累加。正在做的那条不合。 */
function mergeRepeats(items: ActionWords[]): ActionWords[] {
  const merged: Array<ActionWords & { count: number }> = []
  for (const item of items) {
    const last = merged[merged.length - 1]
    if (last && !item.fileResults?.length && !last.fileResults?.length && !item.sources?.length && !last.sources?.length && !item.commandSession && !last.commandSession && item.tool !== 'command_session' && item.state !== 'running' && last.state === item.state && last.text === item.text) {
      last.count += 1
      last.key = item.key
      last.sequence = item.sequence
      last.durationMs = (last.durationMs ?? 0) + (item.durationMs ?? 0)
      if (item.targetPath) last.targetPath = item.targetPath
      if (item.detail && !last.detail) last.detail = item.detail
      else if (item.detail && last.detail && last.detail !== item.detail) {
        last.detail = `${last.detail}\n---\n${item.detail}`
      }
      continue
    }
    merged.push({ ...item, count: 1 })
  }
  return merged.map(({ count, ...item }) => (count > 1 ? { ...item, text: `${item.text}${T.agent.act.times(count)}` } : item))
}

export function changeStatusText(status: string): string {
  return T.changes.status[status] ?? status
}

// ---------------------------------------------------------------------------
// 修改记录的标题和说明：后台起的题目风格不一，作者面前只用一种写法。

/** 机器起的标题先换掉别的叫法、去掉「作者修改」这种前缀（旧版后台起的题目还留在修改记录里）。 */
export function tidyChangeTitle(title: string): string {
  return title
    .replace(/正文候选/gu, '正文')
    .replace(/候选/gu, '修改')
    .replace(/^作者修改\s*/u, '')
    .replace(/[。；;\s]+$/u, '')
    .trim()
}

export interface ChangeHeadline {
  /** 「Agent 改了 README.md」「你改了 3 个文件」。 */
  headline: string
  /** Agent 自己起的题目（说的是改了什么）；你手动保存的那条没有这一行。 */
  detail: string | null
}

/** 标题整个就是一个文件路径（你手动保存的那条、后台照路径起的题目）。 */
const PATH_TITLE = /^[^\s]+[\\/][^\s]*$|^[^\s]+\.[A-Za-z0-9]{1,8}$/u

/**
 * 每条修改记录一个写法：谁 + 改了哪个文件（或几个文件）。
 * 详情里有文件路径，直接用文件名；列表里只有标题和文件数：标题就是路径就用它的文件名，不然写「N 个文件」、题目另起一行。
 */
export function changeHeadline(
  record: { title: string; origin: string; file_count?: number },
  snapshot: ProjectInspection | null,
  paths?: string[],
): ChangeHeadline {
  const title = tidyChangeTitle(record.title)
  const verb = T.changes.verb[record.origin] ?? T.changes.verb.agent
  const titleIsPath = PATH_TITLE.test(title)
  let target: string
  if (paths && paths.length === 1) target = pathDisplayName(snapshot, paths[0])
  else if (paths && paths.length > 1) target = T.changes.files(paths.length)
  else if (titleIsPath) target = pathDisplayName(snapshot, title)
  else target = T.changes.files(record.file_count ?? 1)
  const detail = record.origin !== 'manual' && title.length >= 2 && !titleIsPath && !target.includes(title) ? title : null
  return { headline: `${verb} ${target}`.trim(), detail }
}

export interface ChangeSummaryParts {
  /** 改动说明本身。 */
  text: string
  /** 后台接在后面的写作检查结论，原文；没有就是 null。 */
  note: string | null
}

/** 后台把写作检查的结论接在改动说明后面（「…；AI 味 52 分（medium）；对照…」），这里拆开。 */
/** 旧版手动保存写死的说明（「作者在工作台修改并保存 05_正文内容/…」）是机器套话，不给作者看。 */
const LEGACY_MANUAL_SUMMARY = /^作者在工作台修改并保存/u

export function splitChangeSummary(summary: string | null | undefined): ChangeSummaryParts {
  const raw = (summary ?? '').trim()
  if (LEGACY_MANUAL_SUMMARY.test(raw)) return { text: '', note: null }
  const index = raw.search(/；?\s*(?:AI 味|草稿参考分)\s*\d+/u)
  if (index < 0) return { text: raw, note: null }
  const note = raw.slice(index).replace(/^；\s*/u, '').trim()
  return { text: raw.slice(0, index).replace(/[；\s]+$/u, '').trim(), note: note || null }
}

/**
 * Agent 自己起的题目（detail）和后台的改动说明（summary）常常互相包含
 * （后台爱把题目再抄进说明里），同屏只留一遍：说明里有的不再单独出题目的那行。
 */
export function dedupeChangeLines(detail: string | null | undefined, summaryText: string | null | undefined): { detail: string | null; summary: string } {
  const d = (detail ?? '').trim()
  const s = (summaryText ?? '').trim()
  if (d && s.includes(d)) return { detail: null, summary: s }
  if (d && s && d.includes(s)) return { detail: d, summary: '' }
  return { detail: d || null, summary: s }
}

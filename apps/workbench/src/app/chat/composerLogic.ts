/**
 * 输入框的两种弹出清单：打 @ 引用文件夹里的文件，打 / 用命令或技能。
 * 9-29 重做：/ 清单只有真命令和技能——以前还有「检查矛盾」「通读润色」「顺着往下写」几句写好的话，
 * 作者 9-20：「那种提示或者引导按钮不要有，不适配，不通用」。@ 清单就是文件名和所在文件夹，不再按正文 / 设定 / 人物分类。
 */
import type { ProjectFile } from '../../api/projects'
import { normalizeFilePath, stripFileLocation } from '../lib/fileReferences'
import { markdownFileMentions } from './remarkFileMentions'

// ---------- @ 文件 ----------

export interface MentionTarget {
  query: string
  start: number
  end: number
}

/** 光标前面这个词是不是在打 @：@ 前面不能紧挨字母数字（邮箱不算），@ 后面没有空白、不太长。 */
export function mentionAt(draft: string, cursor: number = draft.length): MentionTarget | null {
  const before = draft.slice(0, cursor)
  const at = before.lastIndexOf('@')
  if (at < 0) return null
  if (at > 0 && /[\w\d]/i.test(before[at - 1])) return null
  const query = before.slice(at + 1)
  if (query.startsWith('"') || query.startsWith("'")) {
    if (query.slice(1).includes(query[0]) || /[\r\n]/.test(query)) return null
    return { query: query.slice(1), start: at, end: cursor }
  }
  if (/\s/.test(query) || query.length > 60) return null
  return { query, start: at, end: cursor }
}

export interface MentionOption {
  fileId: string
  name: string
  folder: string
  path: string
}

function splitPath(path: string): { name: string; folder: string } {
  const parts = path.replace(/\\/g, '/').split('/').filter(Boolean)
  return { name: parts.at(-1) ?? path, folder: parts.slice(0, -1).join('/') }
}

/** 按名字和路径筛；名字开头对上的排前面，再按路径短的排前。最多 8 条。 */
export function mentionOptions(files: readonly ProjectFile[], query: string, limit = 8): MentionOption[] {
  const needle = query.trim().toLowerCase()
  const options = files.map((file) => {
    const path = file.relative_path.replace(/\\/g, '/')
    return { fileId: file.file_id, path, ...splitPath(path) }
  })
  if (!needle) return options.sort((a, b) => a.path.length - b.path.length).slice(0, limit)
  const scored = options.flatMap((option) => {
    const name = option.name.toLowerCase()
    const path = option.path.toLowerCase()
    if (name.startsWith(needle)) return [{ option, score: 0 }]
    if (name.includes(needle)) return [{ option, score: 1 }]
    if (path.includes(needle)) return [{ option, score: 2 }]
    return []
  })
  return scored.sort((a, b) => a.score - b.score || a.option.path.length - b.option.path.length).slice(0, limit).map((item) => item.option)
}

/** 发出去的话里 @ 了哪些文件（按路径、文件名、去掉扩展名的文件名认）→ file_id。 */
export function mentionedFileIds(text: string, files: readonly ProjectFile[]): string[] {
  if (files.length === 0 || !text.includes('@')) return []
  const tokens = markdownFileMentions(text)
  if (!tokens.length || files.length === 0) return []
  const found = new Set<string>()
  for (const raw of tokens) {
    const token = normalizeFilePath(stripFileLocation(raw.path).trim()).toLowerCase()
    if (!token) continue
    const exact = files.find((file) => file.relative_path.replace(/\\/g, '/').toLowerCase() === token || file.file_id === token)
    if (exact) { found.add(exact.file_id); continue }
    const matches = files.filter((file) => {
      const path = file.relative_path.replace(/\\/g, '/').toLowerCase()
      const base = path.split('/').pop() ?? ''
      if (path === token || path.endsWith(`/${token}`) || base === token || base.replace(/\.[a-z0-9]+$/i, '') === token || file.file_id === token) {
        return true
      }
      return false
    })
    // 同名文件有多份时，完整路径才确定是哪一份，不把全部同名文件误附上。
    if (matches.length === 1) found.add(matches[0].file_id)
  }
  return [...found]
}

// ---------- / 命令和技能 ----------

export type CommandAction = 'compact' | 'new-chat' | 'stop' | 'rollback-last' | 'open-changes' | 'pick-model' | 'toggle-readonly'

export interface CommandOption {
  key: string
  label: string
  hint: string
  kind: 'command' | 'skill'
  action?: CommandAction
  /** 拼音 / 英文，打这些字也能找到。 */
  aliases?: string[]
}

export const COMMANDS: CommandOption[] = [
  { key: 'compact', kind: 'command', action: 'compact', label: '压缩对话', hint: '把较早的对话压成一段摘要，后面接着用；原始消息保留', aliases: ['compact', 'yasuo', '整理记忆'] },
  { key: 'new-chat', kind: 'command', action: 'new-chat', label: '新对话', hint: '开一条新对话，这条留着', aliases: ['new', 'xinduihua'] },
  { key: 'stop', kind: 'command', action: 'stop', label: '停止', hint: '停下正在跑的这一轮', aliases: ['stop', 'tingzhi'] },
  { key: 'rollback-last', kind: 'command', action: 'rollback-last', label: '撤回上一次修改', hint: '把这条对话最近一次保存的修改撤回去，撤回前会再问一次', aliases: ['undo', 'chehui'] },
  { key: 'open-changes', kind: 'command', action: 'open-changes', label: '修改记录', hint: '在右栏查看文件修改与撤回记录', aliases: ['changes', 'xiugai'] },
  { key: 'pick-model', kind: 'command', action: 'pick-model', label: '换模型', hint: '打开输入框下面的模型选择', aliases: ['model', 'moxing'] },
  { key: 'toggle-readonly', kind: 'command', action: 'toggle-readonly', label: '切换只读', hint: '在「只读」和「完全访问」之间切换', aliases: ['readonly', 'zhidu'] },
]

/** 光标在不在打 / 命令：整段以 / 开头、还没换行。 */
export function commandQuery(draft: string): string | null {
  if (!draft.startsWith('/') || draft.includes('\n')) return null
  return draft.slice(1)
}

/** 命令在前、技能在后；没打字时技能只露前 6 个。 */
export function commandOptions(query: string, skills: readonly string[]): CommandOption[] {
  const skillOptions: CommandOption[] = skills.map((name) => ({ key: `skill:${name}`, kind: 'skill', label: name, hint: '' }))
  const needle = query.trim().toLowerCase()
  if (!needle) return [...COMMANDS, ...skillOptions.slice(0, 6)]
  return [...COMMANDS, ...skillOptions].filter((option) => (
    option.label.toLowerCase().includes(needle)
    || option.hint.toLowerCase().includes(needle)
    || (option.aliases ?? []).some((alias) => alias.includes(needle))
  ))
}

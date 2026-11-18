/** 同一份文件引用解析用于发送附件、用户消息和 AI 回答。含空格的路径用 @"…"。 */
export interface FileMention { start: number; end: number; path: string; label: string }
export interface FileLocation { line: number; column?: number; endLine?: number; endColumn?: number }
const BARE_NUMBER = /^[+-]?(?:\d+(?:\.\d+)?|\.\d+)$/u

export function fileLocation(href: string): FileLocation | undefined {
  const match = /:(\d+)(?::(\d+))?$/.exec(href) ?? /#L(\d+)(?:C(\d+))?(?:-L?(\d+)(?:C(\d+))?)?$/i.exec(href)
  if (!match) return undefined
  const [, line, column, endLine, endColumn] = match
  const parts = [line, column, endLine, endColumn].filter((part) => part !== undefined).map(Number)
  if (parts.some((part) => !Number.isSafeInteger(part) || part < 1) || (endLine && Number(endLine) < Number(line))) return undefined
  return { line: Number(line), ...(column ? { column: Number(column) } : {}),
    ...(endLine ? { endLine: Number(endLine) } : {}), ...(endColumn ? { endColumn: Number(endColumn) } : {}) }
}

export function formatFileMention(path: string): string {
  return BARE_NUMBER.test(path) || /[\s()（）[\]{}，。！？、"']/u.test(path) ? `@${JSON.stringify(path)}` : `@${path}`
}

export function fileMentions(text: string): FileMention[] {
  const pattern = /(^|[^\w@])@(?:"((?:\\.|[^"\\\r\n])+)"|'([^'\r\n]+)'|([^\s，。！？、()（）[\]{}"'<>]+))/gu
  const found: FileMention[] = []
  for (const match of text.matchAll(pattern)) {
    const start = (match.index ?? 0) + match[1].length
    if (/(?:[a-z][a-z\d+.-]*:\/\/|www\.)[^\s]*$/i.test(text.slice(0, start))) continue
    let path = match[2] ?? match[3] ?? match[4]
    if (match[2] !== undefined) {
      try { path = JSON.parse(`"${match[2]}"`) as string } catch { /* 手写 Windows 路径保留原样。 */ }
    }
    let end = (match.index ?? 0) + match[0].length
    if (match[4]) {
      const clean = path.replace(/[.,;:!?]+$/u, '')
      end -= path.length - clean.length
      path = clean
      // Ordinary unit prices such as "2 items @19.50" are prose. Numeric
      // filenames remain explicit through @"19.50" or @./19.50.
      if (BARE_NUMBER.test(path)) continue
    }
    if (path) found.push({ start, end, path, label: text.slice(start, end) })
  }
  return found
}

/** 内部文件地址交给工作台打开；网址、邮件和页内锚点仍按链接处理。 */
export function isLocalFileHref(href: string): boolean {
  if (!href || href.startsWith('//')) return false
  if (/^(?:file:\/\/|#file\/|[a-z]:[\\/])/i.test(href)) return true
  if (/^(?:https?|ftps?|mailto|tel|sms|data|javascript|vbscript|blob):/i.test(href)) return false
  return !/^(?:[a-z][a-z\d+.-]*:|#|\?)/i.test(stripFileLocation(href))
}

export function decodeFileHref(href: string): string {
  const path = href.replace(/^file:\/\/\/?/i, '').replace(/^#file\//, '')
  try { return decodeURIComponent(path) } catch { return path }
}

/** 实际完整文件名优先；只有 URL 中原样出现的 # 才可能是章节锚点。 */
export function filePathCandidates(href: string): string[] {
  const raw = href.replace(/^file:\/\/\/?/i, '').replace(/^#file\//, '')
  const full = decodeFileHref(raw)
  const candidates = [full, decodeFileHref(stripFileLocation(raw))]
  const fragment = raw.indexOf('#')
  if (fragment >= 0) candidates.push(decodeFileHref(raw.slice(0, fragment)))
  return [...new Set(candidates)]
}

export function stripFileLocation(path: string): string {
  return path.replace(/(?::\d+(?::\d+)?|#L\d+(?:C\d+)?(?:-L?\d+(?:C\d+)?)?)$/i, '')
}

export function normalizeFilePath(raw: string): string {
  const pieces: string[] = []
  for (const part of raw.replace(/\\/g, '/').split('/')) {
    if (!part || part === '.') continue
    if (part === '..' && pieces.length && pieces.at(-1) !== '..') pieces.pop()
    else pieces.push(part)
  }
  return pieces.join('/')
}

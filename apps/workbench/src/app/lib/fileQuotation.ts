import { formatFileMention, type FileLocation } from './fileReferences'

export interface FileQuotation {
  projectId: string
  path: string
  text?: string
  source?: 'original' | 'preview' | 'draft'
  location?: FileLocation
}

export function formatFileQuotation(quote: FileQuotation): string {
  const point = quote.location
  const location = point ? `#L${point.line}C${point.column ?? 1}-L${point.endLine ?? point.line}C${point.endColumn ?? 1}` : ''
  const mention = formatFileMention(quote.path + location)
  if (quote.text === undefined) return mention
  const label = quote.source === 'draft' ? '未保存草稿' : quote.source === 'preview' ? '预览选段' : '文件选段'
  // A quoted document may contain its own Markdown fences.
  let longestFence = 2
  for (const match of quote.text.matchAll(/`+/g)) longestFence = Math.max(longestFence, match[0].length)
  const fence = '`'.repeat(longestFence + 1)
  return `${mention}\n${label}：\n\n${fence}text\n${quote.text}${quote.text.endsWith('\n') ? '' : '\n'}${fence}`
}

export function appendFileQuotation(draft: string, quote: FileQuotation): string {
  return `${draft}${draft && !draft.endsWith('\n\n') ? '\n\n' : ''}${formatFileQuotation(quote)}\n\n`
}

export function selectionLocation(text: string, start: number, end: number): FileLocation {
  const point = (offset: number) => {
    const lines = text.slice(0, offset).split(/\r\n|\r|\n/)
    return { line: lines.length, column: lines.at(-1)!.length + 1 }
  }
  const first = point(start)
  const last = point(Math.max(start, end - 1))
  return { ...first, endLine: last.line, endColumn: last.column }
}

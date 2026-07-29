/** Line-oriented plain text <-> TipTap HTML for chapter prose files. */

export function plainTextToEditorHtml(content: string): string {
  const container = document.createElement('div')
  const normalized = content.replace(/\r\n?/g, '\n')
  const lines = normalized.split('\n')
  if (lines.length > 0 && lines[lines.length - 1] === '') {
    lines.pop()
  }
  if (lines.length === 0) {
    container.append(document.createElement('p'))
    return container.innerHTML
  }

  for (const line of lines) {
    const heading = /^(#{1,3})\s+(.*)$/.exec(line)
    if (heading) {
      const element = document.createElement(
        `h${heading[1].length}` as 'h1' | 'h2' | 'h3',
      )
      element.textContent = heading[2]
      container.append(element)
      continue
    }
    const element = document.createElement('p')
    element.textContent = line
    container.append(element)
  }
  return container.innerHTML
}

function textFromInline(node: {
  type?: string
  text?: string
  content?: Array<{ type?: string; text?: string; content?: unknown[] }>
}): string {
  if (typeof node.text === 'string') return node.text
  if (!Array.isArray(node.content)) return ''
  return node.content.map((child) => textFromInline(child as typeof node)).join('')
}

/** Serialize the live editor document back to project plain text. */
export function editorDocumentToPlainText(doc: {
  content?: Array<{
    type?: string
    attrs?: { level?: number }
    content?: Array<{ type?: string; text?: string; content?: unknown[] }>
  }>
} | null | undefined): string {
  const blocks = doc?.content ?? []
  if (blocks.length === 0) return ''

  const lines: string[] = []
  for (const block of blocks) {
    const type = block.type ?? 'paragraph'
    if (type === 'heading') {
      const level = Math.min(Math.max(block.attrs?.level ?? 1, 1), 3)
      lines.push(`${'#'.repeat(level)} ${textFromInline(block)}`)
      continue
    }
    if (type === 'blockquote') {
      const inner = (block.content ?? [])
        .map((child) => textFromInline(child as typeof block))
        .join('\n')
      if (!inner) {
        lines.push('>')
        continue
      }
      for (const line of inner.split('\n')) {
        lines.push(`> ${line}`)
      }
      continue
    }
    lines.push(textFromInline(block))
  }
  return lines.join('\n')
}

export function shortProsePath(relativePath: string): string {
  const parts = relativePath.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] ?? relativePath
}

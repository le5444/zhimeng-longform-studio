import type { Link, PhrasingContent, Root, Text } from 'mdast'
import { SKIP, visit } from 'unist-util-visit'
import { unified } from 'unified'
import remarkParse from 'remark-parse'
import remarkGfm from 'remark-gfm'
import { fileMentions } from '../lib/fileReferences'

const parser = unified().use(remarkParse).use(remarkGfm)

/** Match the rendered Markdown: quoted code and existing links are not attachments. */
export function markdownFileMentions(source: string) {
  const matches: ReturnType<typeof fileMentions> = []
  visit(parser.parse(source), (node) => {
    if (node.type === 'link' || node.type === 'linkReference') return SKIP
    if (node.type === 'text') matches.push(...fileMentions(node.value))
  })
  return matches
}

/** Convert plain-text file references after Markdown has identified code and links. */
export function remarkFileMentions() {
  return (tree: Root) => {
    visit(tree, (node, index, parent) => {
      if (node.type === 'link' || node.type === 'linkReference') return SKIP
      if (node.type !== 'text' || index === undefined || !parent || !node.value.includes('@')) return
      const matches = fileMentions(node.value)
      if (matches.length === 0) return
      const parts: Array<Text | Link> = []
      let cursor = 0
      for (const match of matches) {
        if (match.start > cursor) parts.push({ type: 'text', value: node.value.slice(cursor, match.start) })
        parts.push({ type: 'link', url: `#file/${encodeURIComponent(match.path)}`, children: [{ type: 'text', value: match.label }] })
        cursor = match.end
      }
      if (cursor < node.value.length) parts.push({ type: 'text', value: node.value.slice(cursor) })
      const children = parent.children as PhrasingContent[]
      children.splice(index, 1, ...parts)
      return index + parts.length
    })
  }
}

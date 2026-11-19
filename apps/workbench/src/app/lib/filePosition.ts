import type { FileLocation } from './fileReferences'

export interface FileFocus { location?: FileLocation; request: number }

export function scrollFilePoint(element: HTMLElement, top: number) {
  const panel = element.closest<HTMLElement>('.zm-panel-body')
  if (!panel) return
  const headerHeight = panel.querySelector('.zm-file-head')?.getBoundingClientRect().height ?? 0
  panel.scrollTop += top - panel.getBoundingClientRect().top - headerHeight - Math.max(0, panel.clientHeight - headerHeight) / 3
}

export function fileTextSelection(text: string, location: FileLocation) {
  const lines = text.split(/\r\n|\r|\n/)
  const count = lines.length - (lines.at(-1) === '' ? 1 : 0)
  if (location.line < 1 || location.line > count) return null
  const line = lines[location.line - 1]
  if (location.column && location.column > line.length + 1) return null
  const starts = [0]
  for (const newline of text.matchAll(/\r\n|\r|\n/g)) starts.push(newline.index + newline[0].length)
  const endLine = Math.min(location.endLine ?? location.line, count)
  const start = starts[location.line - 1] + (location.column ?? 1) - 1
  const end = starts[endLine - 1] + Math.min(location.endColumn ?? lines[endLine - 1].length, lines[endLine - 1].length)
  return { start, end: Math.max(start, end) }
}

export function focusFileEditor(editor: HTMLTextAreaElement, location: FileLocation) {
  // Textareas normalize CRLF; offsets must use their current value, including unsaved edits.
  const selection = fileTextSelection(editor.value, location)
  if (!selection) return
  editor.focus({ preventScroll: true })
  editor.setSelectionRange(selection.start, selection.end)
  const style = getComputedStyle(editor)
  const mirror = document.createElement('div')
  Object.assign(mirror.style, {
    position: 'fixed', visibility: 'hidden', top: '0', left: '0',
    boxSizing: 'border-box', width: `${editor.clientWidth}px`,
    font: style.font, lineHeight: style.lineHeight, letterSpacing: style.letterSpacing,
    padding: style.padding, tabSize: style.tabSize,
    whiteSpace: editor.wrap === 'off' ? 'pre' : 'pre-wrap', overflowWrap: 'break-word',
  })
  const marker = document.createElement('span')
  marker.textContent = editor.value.slice(selection.start, selection.start + 1) || '\u200b'
  mirror.append(editor.value.slice(0, selection.start), marker, editor.value.slice(selection.start + 1))
  document.body.append(mirror)
  try {
    const point = marker.getBoundingClientRect()
    const offset = point.top - mirror.getBoundingClientRect().top
    editor.scrollTop = Math.max(0, offset - editor.clientHeight / 3)
    if (editor.wrap === 'off') editor.scrollLeft = Math.max(0, point.left - mirror.getBoundingClientRect().left - editor.clientWidth / 3)
    scrollFilePoint(editor, editor.getBoundingClientRect().top + offset - editor.scrollTop)
  } finally { mirror.remove() }
}

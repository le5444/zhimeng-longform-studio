import { describe, expect, it } from 'vitest'
import {
  editorDocumentToPlainText,
  plainTextToEditorHtml,
  shortProsePath,
} from './proseDocument'

describe('proseDocument', () => {
  it('round-trips plain chapter text through editor HTML', () => {
    const source = [
      '# 第一章',
      '',
      '雨下了一整夜。',
      '林见川在门口停了很久。',
    ].join('\n')
    const html = plainTextToEditorHtml(source)
    expect(html).toContain('<h1>第一章</h1>')
    expect(html).toContain('<p>雨下了一整夜。</p>')
    expect(html).toContain('<p></p>')

    const doc = {
      content: [
        { type: 'heading', attrs: { level: 1 }, content: [{ type: 'text', text: '第一章' }] },
        { type: 'paragraph' },
        { type: 'paragraph', content: [{ type: 'text', text: '雨下了一整夜。' }] },
        { type: 'paragraph', content: [{ type: 'text', text: '林见川在门口停了很久。' }] },
      ],
    }
    expect(editorDocumentToPlainText(doc)).toBe(source)
  })

  it('keeps the leaf file name for save titles', () => {
    expect(shortProsePath('05_正文内容/第一卷/CH001_正文.md')).toBe('CH001_正文.md')
  })
})

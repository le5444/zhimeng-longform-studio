import { describe, expect, it } from 'vitest'
import type { ProjectChapter } from '../api/projects'
import {
  classifyAgentInstruction,
  parseChapterNumber,
  resolveInstructionChapter,
} from './agentIntent'

const chapters: ProjectChapter[] = [
  {
    chapter_id: 'VOL001-CH001',
    number: 1,
    volume_id: 'VOL001',
    file_ids: ['file-1'],
    prose_file_ids: ['file-1'],
  },
  {
    chapter_id: 'VOL001-CH002',
    number: 2,
    volume_id: 'VOL001',
    file_ids: ['file-2'],
    prose_file_ids: ['file-2'],
  },
]

describe('agent instruction intent', () => {
  it('parses Arabic and Chinese chapter numbers', () => {
    expect(parseChapterNumber('002')).toBe(2)
    expect(parseChapterNumber('十二')).toBe(12)
    expect(parseChapterNumber('一百零二')).toBe(102)
    expect(parseChapterNumber('二〇二')).toBe(202)
  })

  it('routes named-chapter edits to an author-review ChangeSet intent', () => {
    const intent = classifyAgentInstruction('把第二章开头改写得更紧一点')
    expect(intent).toMatchObject({
      writerMode: 'author_review_change_proposal',
      requestedChapterNumber: 2,
      batchChapterCount: null,
      draftsNextChapter: false,
    })
    expect(resolveInstructionChapter(chapters, '把第二章开头改写得更紧一点', 'VOL001-CH001'))
      .toEqual({
        kind: 'resolved',
        chapter: chapters[1],
        requestedNumber: 2,
      })
  })

  it('keeps analysis and distillation requests out of the prose write mode', () => {
    expect(classifyAgentInstruction('只分析第二章并列出修改建议').writerMode)
      .toBe('repair_only')
    expect(classifyAgentInstruction('蒸馏这批资料').writerMode)
      .toBe('repair_only')
  })

  it('recognizes single-next and unsupported batch writing separately', () => {
    expect(classifyAgentInstruction('继续写下一章')).toMatchObject({
      draftsNextChapter: true,
      batchChapterCount: null,
    })
    expect(classifyAgentInstruction('一键写10章')).toMatchObject({
      draftsNextChapter: false,
      batchChapterCount: 10,
    })
  })

  it('reports an explicit missing chapter instead of falling back to the current one', () => {
    expect(resolveInstructionChapter(chapters, '修改第九章', 'VOL001-CH001')).toEqual({
      kind: 'missing',
      chapter: null,
      requestedNumber: 9,
    })
  })
})

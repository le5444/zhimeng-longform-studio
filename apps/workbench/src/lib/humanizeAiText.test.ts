import { describe, expect, it } from 'vitest'
import { humanizeAiText, presentAiText } from './humanizeAiText'

describe('humanizeAiText', () => {
  it('preserves code indentation and repeated blank lines for Markdown rendering', () => {
    const content = '    indented code\n\n```text\nalpha\n\n\nbeta\n```\n'
    expect(presentAiText(content, { preserveMarkdown: true }).answer).toBe(content)
  })

  it('preserves a bilingual document instead of guessing which paragraphs are scratch text', () => {
    const content = 'I will explain the file format.\n\n文件的第一列是日期。'
    expect(presentAiText(content, { preserveMarkdown: true }).answer).toBe(content)
  })

  it('separates explicit leading think tags while preserving the following Markdown', () => {
    const content = '\n\n```text\nalpha\n\n\nbeta\n```\n'
    expect(presentAiText('<think>Checking the report.</think>' + content, { preserveMarkdown: true }))
      .toEqual({ answer: content, thinking: 'Checking the report.' })
  })

  it('removes visible Markdown markers but keeps the readable content', () => {
    expect(humanizeAiText('## 修改建议\n\n- **保留线索**\n\n```text\n把开头写快一点\n```'))
      .toBe('修改建议\n\n保留线索\n\n把开头写快一点')
  })

  it('does not turn ordinary punctuation into a heading or list', () => {
    expect(humanizeAiText('第 3 章：空槽\n\n顾巡走进值房。')).toBe('第 3 章：空槽\n\n顾巡走进值房。')
  })

  it('hides relay scratch preambles and Markdown separators before a Chinese answer', () => {
    expect(humanizeAiText(
      'The user asks me to summarize 第004章. Let me provide a concise summary.\n\n'
      + '## 本章总结\n\n---\n\n- 主角发现了玉佩。',
    )).toBe('本章总结\n\n主角发现了玉佩。')
  })

  it('keeps genuine English-only content', () => {
    expect(humanizeAiText('The user crossed the empty station.')).toBe('The user crossed the empty station.')
  })

  it('removes a relay scratch preamble even when it is glued to the Chinese answer, and keeps the model\'s own words', () => {
    expect(humanizeAiText(
      "I'm in a repair-only Writer mode, so I cannot propose ChangeSets. Let me provide the exact text. 当前处于 repair-only Writer 模式，无法提交 ChangeSet。",
    )).toBe('当前处于 repair-only Writer 模式，无法提交 ChangeSet。')
  })

  it('ignores a quoted Chinese request inside a long relay scratch preamble', () => {
    expect(humanizeAiText(
      'The project audit is degraded. The user task is "你好呀，你知道我现在正在干啥吗". '
      + 'Let me inspect the chapter. I think the user is checking whether I know the project. '
      + 'Let me answer honestly. 你好呀，我知道。你正在修改《宗门只剩七个人》的第四章。',
    )).toBe('你好呀，我知道。你正在修改《宗门只剩七个人》的第四章。')
  })

  it('keeps bilingual prose that is not an internal scratch preamble', () => {
    expect(humanizeAiText('Good morning，沈青云说。')).toBe('Good morning，沈青云说。')
  })

  it('把中转站混进回答字段的 think 内容单独拿出来', () => {
    expect(presentAiText(
      '<think>我们被问到一个直接问题。先判断作者要不要改正文。</think>\n'
      + '最怕开篇就堆满设定，让读者还没进入故事就失去耐心。',
    )).toEqual({
      answer: '最怕开篇就堆满设定，让读者还没进入故事就失去耐心。',
      thinking: '我们被问到一个直接问题。先判断作者要不要改正文。',
    })
  })

  it('流式阶段只有未闭合 think 标签时不把思考显示成回答', () => {
    expect(presentAiText('<think>正在核对章节设定。')).toEqual({
      answer: '',
      thinking: '正在核对章节设定。',
    })
  })

  it('没有 think 标签时保持原来的回答', () => {
    expect(presentAiText('Good morning，沈青云说。')).toEqual({
      answer: 'Good morning，沈青云说。',
      thinking: null,
    })
  })

  it('回答中间提到 think 标签时不误当成模型思考', () => {
    expect(presentAiText('可以把 <think>示例内容</think> 当作普通说明。')).toEqual({
      answer: '可以把 示例内容 当作普通说明。',
      thinking: null,
    })
  })
})

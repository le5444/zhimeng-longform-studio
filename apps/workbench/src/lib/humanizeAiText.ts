export interface AiTextPresentation {
  answer: string
  thinking: string | null
}

/**
 * 中转站偶尔把模型思考包在 <think> 里，混进普通回答字段。显示时把它
 * 和最终回答拆开；这里只处理界面文本，不会改动 ChangeSet 或作品文件。
 */
export function presentAiText(content: string, options: { preserveMarkdown?: boolean } = {}): AiTextPresentation {
  const tagged = extractTaggedThinking(content)
  return {
    answer: humanizeAnswer(tagged.answer, options.preserveMarkdown),
    thinking: tagged.thinking
      ? normalizeVisibleText(tagged.thinking).trim() || null
      : null,
  }
}

/** AI 输出在作者界面里只保留内容本身，不把 Markdown 语法当成正文展示。 */
export function humanizeAiText(content: string): string {
  return presentAiText(content).answer
}

function humanizeAnswer(content: string, preserveMarkdown = false): string {
  // Whitespace and bilingual text are part of the document passed to the parser.
  if (preserveMarkdown) return content
  let readable = normalizeVisibleText(content)

  // Some OpenAI-compatible relays put an English scratch preamble into the
  // normal content field. It is not the answer and should not be presented as
  // a model "thought". Only remove leading meta paragraphs when a Chinese
  // answer follows, so genuine English fiction/content remains untouched.
  if (/[\u3400-\u9fff]/.test(readable)) {
    const paragraphs = readable.split(/\n{2,}/)
    while (
      paragraphs.length > 1
      && /^(?:The user\b|I(?:'ve| have| will| need| should| can)\b|Let me\b)/i.test(paragraphs[0].trim())
    ) {
      paragraphs.shift()
    }
    readable = paragraphs.join('\n\n').trim()

    const scratchCut = relayScratchPreambleEnd(readable)
    if (scratchCut !== null) {
      readable = readable.slice(scratchCut).trim()
    }

    // A few relays concatenate the English scratch preamble and the Chinese
    // answer into one paragraph. Remove only a clearly meta-looking prefix;
    // genuine bilingual prose remains untouched.
    const firstChinese = readable.search(/[\u3400-\u9fff]/)
    if (firstChinese > 0) {
      const prefix = readable.slice(0, firstChinese).trim()
      if (
        /^(?:The user\b|I(?:'m|'ve| have| will| need| should| can)\b|Let me\b)/i.test(prefix)
        && /\b(?:user|mode|tool|file|change|respond|provide|cannot|can't|need|should)\b/i.test(prefix)
      ) {
        readable = readable.slice(firstChinese).trim()
      }
    }
  }

  return readable.trim()
}

function normalizeVisibleText(content: string): string {
  return content
    .replace(/\r\n?/g, '\n')
    .replace(/^[ \t]*```[^\n]*\n?/gm, '')
    .replace(/^[ \t]*```[ \t]*$/gm, '')
    .replace(/^[ \t]{0,3}#{1,6}[ \t]+/gm, '')
    .replace(/^[ \t]{0,3}>[ \t]?/gm, '')
    .replace(/^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)/gm, '')
    .replace(/^[ \t]*[-*_]{3,}[ \t]*$/gm, '')
    .replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1')
    .replace(/\[([^\]]+)\]\([^)]*\)/g, '$1')
    .replace(/`([^`]+)`/g, '$1')
    .replace(/\*{1,3}([^*\n]+)\*{1,3}/g, '$1')
    .replace(/_{1,3}([^_\n]+)_{1,3}/g, '$1')
    .replace(/~~([^~\n]+)~~/g, '$1')
    .replace(/^[ \t]*(?:[-*+][ \t]+|\d+[.)][ \t]+)/gm, '')
    .replace(/<[^>]+>/g, '')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}



function extractTaggedThinking(content: string): { answer: string; thinking: string | null } {
  const thinkingParts: string[] = []
  let answer = content

  // 只消费输出开头连续出现的思考块。正文或代码中间作为示例出现的
  // <think> 必须继续留在回答里，不能因为标签同名就把内容搬走。
  while (true) {
    const start = answer.match(/^\s*<think\b[^>]*>/i)
    if (!start) break
    const afterStart = answer.slice(start[0].length)
    const end = afterStart.match(/<\/think\s*>/i)
    if (!end || end.index === undefined) {
      if (afterStart.trim()) thinkingParts.push(afterStart)
      answer = ''
      break
    }
    const thinking = afterStart.slice(0, end.index)
    if (thinking.trim()) thinkingParts.push(thinking)
    answer = afterStart.slice(end.index + end[0].length)
  }

  const thinking = thinkingParts.join('\n\n').trim()
  return { answer, thinking: thinking || null }
}

/**
 * Some relays put several English planning sentences in `content` instead of
 * the reasoning channel. Start after the last clear planning cue, otherwise a
 * quoted Chinese user request near the beginning would be mistaken for the
 * final answer. Two cues are required so ordinary bilingual prose is kept.
 */
function relayScratchPreambleEnd(content: string): number | null {
  const cue = /\b(?:let me|the user|the task|the project audit|looking at|look at|i (?:think|need|should|will|can)|system context|formal draft|tool calls?|change set)\b/gi
  let cueCount = 0
  let lastCueEnd = 0
  for (const match of content.matchAll(cue)) {
    cueCount += 1
    lastCueEnd = (match.index ?? 0) + match[0].length
  }
  if (cueCount < 2 || lastCueEnd === 0) return null

  const remainder = content.slice(lastCueEnd)
  const chineseOffset = remainder.search(/[\u3400-\u9fff]/)
  if (chineseOffset < 0) return null

  const answerStart = lastCueEnd + chineseOffset
  const answer = content.slice(answerStart)
  const chineseCharacters = answer.match(/[\u3400-\u9fff]/g)?.length ?? 0
  return chineseCharacters >= 8 ? answerStart : null
}

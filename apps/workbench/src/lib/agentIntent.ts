import type { WriterMode } from '../api/agent'
import type { ProjectChapter } from '../api/projects'

const CHINESE_DIGITS: Record<string, number> = {
  零: 0,
  〇: 0,
  一: 1,
  二: 2,
  两: 2,
  三: 3,
  四: 4,
  五: 5,
  六: 6,
  七: 7,
  八: 8,
  九: 9,
}

const SMALL_UNITS: Record<string, number> = {
  十: 10,
  百: 100,
  千: 1000,
}

export interface AgentInstructionIntent {
  writerMode: WriterMode
  requestedChapterNumber: number | null
  batchChapterCount: number | null
  draftsNextChapter: boolean
}

export type ChapterTargetResolution =
  | { kind: 'implicit'; chapter: null; requestedNumber: null }
  | { kind: 'resolved'; chapter: ProjectChapter; requestedNumber: number }
  | { kind: 'missing'; chapter: null; requestedNumber: number }
  | { kind: 'ambiguous'; chapter: null; requestedNumber: number }

function normalizeArabicDigits(value: string): string {
  return value.replace(/[０-９]/g, (digit) => String(digit.charCodeAt(0) - 0xFF10))
}

export function parseChapterNumber(value: string): number | null {
  const normalized = normalizeArabicDigits(value.trim()).replaceAll('两', '二')
  if (!normalized) return null
  if (/^\d+$/.test(normalized)) {
    const number = Number.parseInt(normalized, 10)
    return Number.isSafeInteger(number) && number > 0 ? number : null
  }

  const characters = [...normalized]
  if (characters.every((character) => character in CHINESE_DIGITS)) {
    const number = Number.parseInt(
      characters.map((character) => CHINESE_DIGITS[character]).join(''),
      10,
    )
    return Number.isSafeInteger(number) && number > 0 ? number : null
  }

  let total = 0
  let section = 0
  let digit = 0
  for (const character of characters) {
    if (character in CHINESE_DIGITS) {
      digit = CHINESE_DIGITS[character]
      continue
    }
    if (character in SMALL_UNITS) {
      section += (digit || 1) * SMALL_UNITS[character]
      digit = 0
      continue
    }
    if (character === '万') {
      total += (section + digit) * 10_000
      section = 0
      digit = 0
      continue
    }
    return null
  }
  const number = total + section + digit
  return Number.isSafeInteger(number) && number > 0 ? number : null
}

export function requestedChapterNumber(instruction: string): number | null {
  const explicit = instruction.match(
    /第\s*([0-9０-９零〇一二两三四五六七八九十百千万]+)\s*[章张]/i,
  )
  if (explicit) return parseChapterNumber(explicit[1])

  const chapterId = instruction.match(/\bCH[-_\s]?0*(\d+)\b/i)
  return chapterId ? parseChapterNumber(chapterId[1]) : null
}

function requestedBatchCount(instruction: string): number | null {
  const match = instruction.match(
    /(?:一键|连续|批量|接着|继续|再|帮我)?\s*(?:写|续写|生成)\s*([0-9０-９零〇一二两三四五六七八九十百千万]+)\s*[章张]/i,
  )
  const count = match ? parseChapterNumber(match[1]) : null
  return count !== null && count > 1 ? count : null
}

function wantsDraftNextChapter(instruction: string): boolean {
  return /(?:续写|继续写|接着写|写)\s*(?:下|下一)\s*[章张]/i.test(instruction)
}

export function inferWriterMode(instruction: string): WriterMode {
  const analysisOnly =
    /(?:先别|不要|不用)\s*(?:改|修改|改写|重写|写回|动文件)|只(?:做|要)?\s*(?:分析|审读|审稿|检查|查找|搜索|列出|给建议)|(?:给出|列出)\s*(?:修改)?建议|蒸馏|提取资料/i
  if (analysisOnly.test(instruction)) return 'repair_only'

  const changesProse =
    /修改|改写|重写|润色|续写|扩写|补写|删掉|删除|替换|改成|收紧|去\s*AI\s*味|统一\s*(?:文风|语气|人称)|调整\s*(?:开头|结尾|段落|正文|章节)/i
  return changesProse.test(instruction)
    ? 'author_review_change_proposal'
    : 'repair_only'
}

export function classifyAgentInstruction(instruction: string): AgentInstructionIntent {
  return {
    writerMode: inferWriterMode(instruction),
    requestedChapterNumber: requestedChapterNumber(instruction),
    batchChapterCount: requestedBatchCount(instruction),
    draftsNextChapter: wantsDraftNextChapter(instruction),
  }
}

export function resolveInstructionChapter(
  chapters: ProjectChapter[],
  instruction: string,
  activeChapterId: string | null,
): ChapterTargetResolution {
  const requestedNumber = requestedChapterNumber(instruction)
  if (requestedNumber === null) {
    return { kind: 'implicit', chapter: null, requestedNumber: null }
  }

  const matches = chapters.filter((chapter) => chapter.number === requestedNumber)
  if (matches.length === 1) {
    return { kind: 'resolved', chapter: matches[0], requestedNumber }
  }
  if (matches.length === 0) {
    return { kind: 'missing', chapter: null, requestedNumber }
  }

  const activeChapter = chapters.find((chapter) => chapter.chapter_id === activeChapterId)
  const sameVolume = activeChapter
    ? matches.filter((chapter) => chapter.volume_id === activeChapter.volume_id)
    : []
  if (sameVolume.length === 1) {
    return { kind: 'resolved', chapter: sameVolume[0], requestedNumber }
  }
  return { kind: 'ambiguous', chapter: null, requestedNumber }
}

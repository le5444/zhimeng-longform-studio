import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface ChapterDraft {
  schema: 'zhimeng.chapter-draft.v1'
  change_set_id: string
  relative_path: string
  chapter_number: number
  characters: number
  model: string
  /** 恒为 false：候选不会自己写进小说，必须作者批准。 */
  applied: boolean
  requires_author_review: boolean
}

export interface ChapterDraftInput {
  baseRevision: number
  baseFingerprint: string
  targetCharacters?: number
  instruction?: string | null
}

/**
 * 写一章要跑几十秒到几分钟，远超 Runtime 客户端默认的 3.5 秒超时，
 * 所以这里显式放宽；否则请求会在模型还没写完时就被 abort 掉。
 */
const DRAFT_TIMEOUT_MS = 420_000

/**
 * 让写作模型续写下一章，产出**待作者审阅**的 ChangeSet。
 * 这个调用不会修改小说文件；作者在 Diff 里接受并 Apply 之后才写盘。
 */
export function draftNextChapter(
  projectId: string,
  input: ChapterDraftInput,
  options: RuntimeClientOptions = {},
): Promise<ChapterDraft> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/chapter-drafts`,
    { timeoutMs: DRAFT_TIMEOUT_MS, ...options },
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_revision: input.baseRevision,
        base_fingerprint: input.baseFingerprint,
        target_characters: input.targetCharacters ?? 2200,
        instruction: input.instruction ?? null,
      }),
    },
  )
}

export interface PassageRewrite {
  schema: 'zhimeng.passage-rewrite.v1'
  change_set_id: string
  relative_path: string
  original_characters: number
  rewritten_characters: number
  model: string
  ai_smell: { score: number; level: string } | null
  applied: boolean
  requires_author_review: boolean
}

export interface PassageRewriteInput {
  baseRevision: number
  baseFingerprint: string
  relativePath: string
  fileId: string
  passage: string
  instruction: string
}

/**
 * 按作者要求重写选中的一段，产出**待审阅**的 ChangeSet。
 * 后端 fail closed：原文对不上、或在本章出现多次，都会拒绝而不是瞎猜。
 */
export function rewritePassage(
  projectId: string,
  input: PassageRewriteInput,
  options: RuntimeClientOptions = {},
): Promise<PassageRewrite> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/passage-rewrites`,
    { timeoutMs: DRAFT_TIMEOUT_MS, ...options },
    {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        base_revision: input.baseRevision,
        base_fingerprint: input.baseFingerprint,
        relative_path: input.relativePath,
        file_id: input.fileId,
        passage: input.passage,
        instruction: input.instruction,
      }),
    },
  )
}

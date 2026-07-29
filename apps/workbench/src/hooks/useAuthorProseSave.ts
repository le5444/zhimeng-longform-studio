import { useCallback, useRef, useState } from 'react'
import {
  applyChangeSet,
  createProjectChangeSet,
  runPostDraftGate,
  updateHunkDecisions,
  type ChangeSet,
} from '../api/changeSets'
import type { ProjectFileContent } from '../api/projects'
import { RuntimeApiError } from '../api/runtime'
import { shortProsePath } from '../lib/proseDocument'

export type AuthorProseSaveState = 'idle' | 'saving' | 'error'

export interface AuthorProseSaveTarget {
  projectId: string
  baseRevision: number
  baseFingerprint: string
  fileId: string
  relativePath: string
  beforeContent: string
}

function saveErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown; code?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
    if (error.status === 409) return '项目或正文已变化，请刷新后重试'
    return error.message
  }
  return error instanceof Error ? error.message : '正文写回失败'
}

export async function saveAuthorProseViaChangeSet(
  target: AuthorProseSaveTarget,
  afterContent: string,
): Promise<ChangeSet> {
  if (afterContent === target.beforeContent) {
    throw new Error('正文没有变化')
  }

  const created = await createProjectChangeSet(target.projectId, {
    base_revision: target.baseRevision,
    base_fingerprint: target.baseFingerprint,
    title: `作者修改 ${shortProsePath(target.relativePath)}`,
    summary: `作者在工作台直接编辑并写回 ${target.relativePath}`,
    origin: 'manual',
    gate_required: false,
    changes: [{
      operation: 'update',
      file_id: target.fileId,
      after_content: afterContent,
      rationale: '作者手改正文。',
    }],
  })

  const decisions = created.changes.flatMap((change) => (
    change.hunks.map((hunk) => ({
      hunk_id: hunk.hunk_id,
      decision: 'accepted' as const,
    }))
  ))

  let current = created
  if (decisions.length > 0) {
    current = await updateHunkDecisions(
      created.change_set_id,
      created.review_revision,
      decisions,
    )
  }

  if (current.gate_required) {
    current = await runPostDraftGate(current.change_set_id, current.review_revision)
    const receipt = current.post_draft_gate
    if (!receipt || receipt.status !== 'passed') {
      const detail = receipt?.result?.summary
        ?? receipt?.error_message
        ?? '正文 Gate 未通过，已保留 ChangeSet 供审阅'
      throw new Error(detail)
    }
  }

  return applyChangeSet(current.change_set_id, current.review_revision)
}

export function useAuthorProseSave(options: {
  content: ProjectFileContent | null
  enabled: boolean
  onSaved?: (changeSet: ChangeSet) => void
}) {
  const [saveState, setSaveState] = useState<AuthorProseSaveState>('idle')
  const [saveError, setSaveError] = useState<string | null>(null)
  const [lastChangeSetId, setLastChangeSetId] = useState<string | null>(null)
  const inflight = useRef(false)
  const contentRef = useRef(options.content)
  contentRef.current = options.content
  const enabledRef = useRef(options.enabled)
  enabledRef.current = options.enabled
  const onSavedRef = useRef(options.onSaved)
  onSavedRef.current = options.onSaved

  const clearError = useCallback(() => {
    setSaveError(null)
    setSaveState((current) => (current === 'error' ? 'idle' : current))
  }, [])

  const save = useCallback(async (afterContent: string) => {
    const content = contentRef.current
    if (!enabledRef.current || !content || inflight.current) return null

    inflight.current = true
    setSaveState('saving')
    setSaveError(null)
    try {
      const applied = await saveAuthorProseViaChangeSet({
        projectId: content.project_id,
        baseRevision: content.snapshot_revision,
        baseFingerprint: content.project_fingerprint,
        fileId: content.file.file_id,
        relativePath: content.file.relative_path,
        beforeContent: content.content,
      }, afterContent)
      setLastChangeSetId(applied.change_set_id)
      setSaveState('idle')
      onSavedRef.current?.(applied)
      return applied
    } catch (error) {
      setSaveState('error')
      setSaveError(saveErrorMessage(error))
      return null
    } finally {
      inflight.current = false
    }
  }, [])

  return {
    save,
    saveState,
    saveError,
    lastChangeSetId,
    clearError,
    canSave: options.enabled && options.content !== null && saveState !== 'saving',
  }
}

import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  finalizeKnowledgeSourceImport,
  finalizeProjectKnowledgeSourceImport,
  getKnowledgeSourceFormalization,
  getProjectKnowledgeSourceFormalization,
  importKnowledgeCandidates,
  importProjectKnowledgeCandidates,
  listKnowledgeCandidates,
  listKnowledgeCards,
  listProjectKnowledgeCandidates,
  listProjectKnowledgeCards,
  promoteKnowledgeCandidate,
  promoteProjectKnowledgeCandidate,
  rejectKnowledgeCandidate,
  rejectProjectKnowledgeCandidate,
  rollbackKnowledgeCard,
  rollbackProjectKnowledgeCard,
  setKnowledgeCardStatus,
  setProjectKnowledgeCardStatus,
  updateKnowledgeCandidate,
  updateProjectKnowledgeCandidate,
} from '../api/knowledge'
import { RuntimeApiError } from '../api/runtime'
import { getSourceImportExecutionReceipt } from '../api/sourceImports'
import {
  knowledgeAwaitingFormalizationFixture,
  knowledgeCandidateFixture,
  knowledgeCandidateListFixture,
  knowledgeCardFixture,
  knowledgeCardListFixture,
  knowledgeFormalizationFixture,
  knowledgeFormalizationReceiptFixture,
  knowledgeProjectScopeFixture,
  knowledgeScopedCardListFixture,
  knowledgeSourceReceiptFixture,
} from '../test/knowledgeFixture'
import { useKnowledgeReview } from './useKnowledgeReview'

vi.mock('../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/knowledge')>()
  return {
    ...actual,
    finalizeKnowledgeSourceImport: vi.fn(),
    finalizeProjectKnowledgeSourceImport: vi.fn(),
    getKnowledgeSourceFormalization: vi.fn(),
    getProjectKnowledgeSourceFormalization: vi.fn(),
    importKnowledgeCandidates: vi.fn(),
    importProjectKnowledgeCandidates: vi.fn(),
    listKnowledgeCandidates: vi.fn(),
    listKnowledgeCards: vi.fn(),
    listProjectKnowledgeCandidates: vi.fn(),
    listProjectKnowledgeCards: vi.fn(),
    promoteKnowledgeCandidate: vi.fn(),
    promoteProjectKnowledgeCandidate: vi.fn(),
    rejectKnowledgeCandidate: vi.fn(),
    rejectProjectKnowledgeCandidate: vi.fn(),
    rollbackKnowledgeCard: vi.fn(),
    rollbackProjectKnowledgeCard: vi.fn(),
    setKnowledgeCardStatus: vi.fn(),
    setProjectKnowledgeCardStatus: vi.fn(),
    updateKnowledgeCandidate: vi.fn(),
    updateProjectKnowledgeCandidate: vi.fn(),
  }
})

vi.mock('../api/sourceImports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/sourceImports')>()
  return {
    ...actual,
    getSourceImportExecutionReceipt: vi.fn(),
  }
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason?: unknown) => void
  const promise = new Promise<T>((nextResolve, nextReject) => {
    resolve = nextResolve
    reject = nextReject
  })
  return { promise, resolve, reject }
}

describe('useKnowledgeReview', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(importKnowledgeCandidates).mockResolvedValue(knowledgeCandidateListFixture)
    vi.mocked(listKnowledgeCandidates).mockResolvedValue(knowledgeCandidateListFixture)
    vi.mocked(listKnowledgeCards).mockResolvedValue(knowledgeCardListFixture)
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(knowledgeSourceReceiptFixture)
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(knowledgeAwaitingFormalizationFixture)
    vi.mocked(finalizeKnowledgeSourceImport).mockResolvedValue(knowledgeFormalizationReceiptFixture)
    vi.mocked(promoteKnowledgeCandidate).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(rejectKnowledgeCandidate).mockResolvedValue({
      ...knowledgeCandidateFixture,
      status: 'rejected',
      revision: 2,
    })
    vi.mocked(rollbackKnowledgeCard).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(setKnowledgeCardStatus).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(updateKnowledgeCandidate).mockResolvedValue(knowledgeCandidateFixture)
    vi.mocked(finalizeProjectKnowledgeSourceImport).mockResolvedValue(
      knowledgeFormalizationReceiptFixture,
    )
    vi.mocked(getProjectKnowledgeSourceFormalization).mockResolvedValue(
      knowledgeAwaitingFormalizationFixture,
    )
    vi.mocked(importProjectKnowledgeCandidates).mockResolvedValue(knowledgeCandidateListFixture)
    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue(knowledgeCandidateListFixture)
    vi.mocked(listProjectKnowledgeCards).mockResolvedValue(knowledgeScopedCardListFixture)
    vi.mocked(promoteProjectKnowledgeCandidate).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(rejectProjectKnowledgeCandidate).mockResolvedValue({
      ...knowledgeCandidateFixture,
      status: 'rejected',
      revision: 2,
    })
    vi.mocked(rollbackProjectKnowledgeCard).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(setProjectKnowledgeCardStatus).mockResolvedValue(knowledgeCardFixture)
    vi.mocked(updateProjectKnowledgeCandidate).mockResolvedValue(knowledgeCandidateFixture)
  })

  it('preserves a 409 status and clears it after reloading current state', async () => {
    vi.mocked(promoteKnowledgeCandidate).mockRejectedValueOnce(new RuntimeApiError(409, {
      error: { message: 'knowledge candidate changed before promotion' },
    }))
    const { result } = renderHook(() => useKnowledgeReview())
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => {
      await expect(result.current.promote(knowledgeCandidateFixture)).rejects.toBeInstanceOf(
        RuntimeApiError,
      )
    })
    expect(result.current.errorStatus).toBe(409)
    expect(result.current.error).toBe('knowledge candidate changed before promotion')

    await act(async () => {
      await result.current.refresh()
    })
    expect(result.current.errorStatus).toBeNull()
    expect(result.current.error).toBeNull()
    expect(result.current.state).toBe('ready')
  })

  it('binds explicit replacement and rollback to the selected records', async () => {
    const olderVersion = knowledgeCardFixture.active_version
    const currentVersion = {
      ...olderVersion,
      version_id: `knowledge-version-${'f'.repeat(32)}`,
      version_number: 2,
      content_hash: '3'.repeat(64),
    }
    const versionedCard = {
      ...knowledgeCardFixture,
      state_revision: 2,
      active_version_id: currentVersion.version_id,
      active_version: currentVersion,
      versions: [currentVersion, olderVersion],
    }
    const rolledBackCard = {
      ...versionedCard,
      state_revision: 3,
      active_version_id: olderVersion.version_id,
      active_version: olderVersion,
    }
    vi.mocked(rollbackKnowledgeCard).mockResolvedValue(rolledBackCard)
    vi.mocked(listKnowledgeCards)
      .mockResolvedValueOnce({ ...knowledgeCardListFixture, cards: [versionedCard] })
      .mockResolvedValue({
        ...knowledgeCardListFixture,
        cards: [rolledBackCard],
        fingerprint: '4'.repeat(64),
      })
    const { result } = renderHook(() => useKnowledgeReview())
    await waitFor(() => expect(result.current.state).toBe('ready'))

    await act(async () => {
      await result.current.promote(knowledgeCandidateFixture, true)
    })
    expect(promoteKnowledgeCandidate).toHaveBeenCalledWith(knowledgeCandidateFixture, true)

    await act(async () => {
      await result.current.rollback(versionedCard, olderVersion.version_id)
    })
    expect(rollbackKnowledgeCard).toHaveBeenCalledWith(versionedCard, olderVersion.version_id)
    expect(result.current.cards[0]?.active_version_id).toBe(olderVersion.version_id)
    expect(result.current.fingerprint).toBe('4'.repeat(64))
  })

  it('loads batch lineage and finalizes with all three immutable hashes', async () => {
    const terminalCandidate = {
      ...knowledgeCandidateFixture,
      status: 'promoted' as const,
      promoted_version_id: knowledgeCardFixture.active_version_id,
    }
    vi.mocked(listKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [terminalCandidate],
    })
    vi.mocked(getKnowledgeSourceFormalization)
      .mockResolvedValueOnce(knowledgeAwaitingFormalizationFixture)
      .mockResolvedValue(knowledgeFormalizationFixture)
    const { result } = renderHook(() => useKnowledgeReview(
      true,
      knowledgeCandidateFixture.source_import_id,
    ))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    expect(result.current.candidateSetHash).toBe(knowledgeCandidateListFixture.candidate_set_hash)
    expect(result.current.sourceReceipt).toEqual(knowledgeSourceReceiptFixture)
    expect(result.current.formalization?.status).toBe('awaiting_review')

    await act(async () => {
      await result.current.finalize()
    })

    expect(finalizeKnowledgeSourceImport).toHaveBeenCalledWith(
      knowledgeCandidateFixture.source_import_id,
      {
        expected_source_receipt_hash: knowledgeSourceReceiptFixture.receipt?.receipt_hash,
        expected_draft_hash: knowledgeSourceReceiptFixture.receipt?.cards_draft_hash,
        expected_candidate_set_hash: knowledgeCandidateListFixture.candidate_set_hash,
      },
    )
    expect(result.current.formalization?.status).toBe('verified')
  })

  it('routes project reviews through scoped clients and exposes the scope fingerprint', async () => {
    const projectId = knowledgeProjectScopeFixture.project_binding_id as string
    const { result } = renderHook(() => useKnowledgeReview(
      true,
      knowledgeCandidateFixture.source_import_id,
      projectId,
    ))
    await waitFor(() => expect(result.current.state).toBe('ready'))

    expect(listProjectKnowledgeCandidates).toHaveBeenCalledWith(
      projectId,
      knowledgeCandidateFixture.source_import_id,
    )
    expect(listProjectKnowledgeCards).toHaveBeenCalledWith(projectId)
    expect(getProjectKnowledgeSourceFormalization).toHaveBeenCalledWith(
      projectId,
      knowledgeCandidateFixture.source_import_id,
    )
    expect(listKnowledgeCandidates).not.toHaveBeenCalled()
    expect(listKnowledgeCards).not.toHaveBeenCalled()
    expect(result.current.scope).toEqual(knowledgeProjectScopeFixture)
    expect(result.current.scopeLabel).toBe('项目知识范围（含应用基础）')
    expect(result.current.scopeFingerprint).toBe(knowledgeScopedCardListFixture.fingerprint)
    expect(result.current.inheritedCardIds).toEqual(
      knowledgeScopedCardListFixture.inherited_card_ids,
    )

    await act(async () => {
      await result.current.promote(knowledgeCandidateFixture, true)
    })
    expect(promoteProjectKnowledgeCandidate).toHaveBeenCalledWith(
      projectId,
      knowledgeCandidateFixture,
      true,
    )
    expect(promoteKnowledgeCandidate).not.toHaveBeenCalled()
  })

  it('ignores a previous project load that resolves after the active scope changes', async () => {
    const projectA = 'project-a'
    const projectB = 'project-b'
    const projectACards = deferred<typeof knowledgeScopedCardListFixture>()
    const scopeFor = (projectId: string) => ({
      ...knowledgeProjectScopeFixture,
      scope_id: `knowledge-scope-${projectId}`,
      project_binding_id: projectId,
    })
    const cardFor = (projectId: string) => ({
      ...knowledgeCardFixture,
      card_id: `card-${projectId}`,
    })
    const cardListFor = (projectId: string, fingerprint: string) => ({
      ...knowledgeScopedCardListFixture,
      scope: scopeFor(projectId),
      cards: [cardFor(projectId)],
      inherited_card_ids: [],
      fingerprint,
    })

    vi.mocked(listProjectKnowledgeCandidates).mockResolvedValue({
      ...knowledgeCandidateListFixture,
      candidates: [],
      candidate_set_hash: null,
    })
    vi.mocked(listProjectKnowledgeCards).mockImplementation((projectId) => (
      projectId === projectA
        ? projectACards.promise
        : Promise.resolve(cardListFor(projectB, '2'.repeat(64)))
    ))

    const { result, rerender } = renderHook(
      ({ projectId }) => useKnowledgeReview(true, null, projectId),
      { initialProps: { projectId: projectA } },
    )
    await waitFor(() => expect(listProjectKnowledgeCards).toHaveBeenCalledWith(projectA))

    rerender({ projectId: projectB })
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.cards[0]?.card_id).toBe(`card-${projectB}`)
    expect(result.current.scope?.project_binding_id).toBe(projectB)
    expect(result.current.scopeFingerprint).toBe('2'.repeat(64))

    await act(async () => {
      projectACards.resolve(cardListFor(projectA, '1'.repeat(64)))
      await projectACards.promise
    })

    expect(result.current.cards[0]?.card_id).toBe(`card-${projectB}`)
    expect(result.current.scope?.project_binding_id).toBe(projectB)
    expect(result.current.scopeFingerprint).toBe('2'.repeat(64))
  })

  it('does not let a completed action from the previous project overwrite the new project', async () => {
    const projectA = 'project-a'
    const projectB = 'project-b'
    const stalePromotion = deferred<typeof knowledgeCardFixture>()
    const scopeFor = (projectId: string) => ({
      ...knowledgeProjectScopeFixture,
      scope_id: `knowledge-scope-${projectId}`,
      project_binding_id: projectId,
    })
    const candidateFor = (projectId: string) => ({
      ...knowledgeCandidateFixture,
      candidate_id: `knowledge-candidate-${projectId}`,
      card_id: `card-${projectId}`,
    })
    const cardFor = (projectId: string) => ({
      ...knowledgeCardFixture,
      card_id: `card-${projectId}`,
    })
    const candidateListFor = (projectId: string) => ({
      ...knowledgeCandidateListFixture,
      candidates: [candidateFor(projectId)],
      candidate_set_hash: projectId === projectA ? '1'.repeat(64) : '2'.repeat(64),
    })
    const cardListFor = (projectId: string) => ({
      ...knowledgeScopedCardListFixture,
      scope: scopeFor(projectId),
      cards: [cardFor(projectId)],
      inherited_card_ids: [],
      fingerprint: projectId === projectA ? '3'.repeat(64) : '4'.repeat(64),
    })

    vi.mocked(listProjectKnowledgeCandidates).mockImplementation(async (projectId) => (
      candidateListFor(projectId)
    ))
    vi.mocked(listProjectKnowledgeCards).mockImplementation(async (projectId) => (
      cardListFor(projectId)
    ))
    vi.mocked(promoteProjectKnowledgeCandidate).mockImplementation((projectId) => (
      projectId === projectA
        ? stalePromotion.promise
        : Promise.resolve(cardFor(projectId))
    ))

    const { result, rerender } = renderHook(
      ({ projectId }) => useKnowledgeReview(true, null, projectId),
      { initialProps: { projectId: projectA } },
    )
    await waitFor(() => expect(result.current.state).toBe('ready'))
    const projectACandidate = result.current.candidates[0]
    expect(projectACandidate?.card_id).toBe(`card-${projectA}`)

    let promotionPromise: ReturnType<typeof result.current.promote> | undefined
    act(() => {
      promotionPromise = result.current.promote(projectACandidate!, true)
    })
    await waitFor(() => expect(result.current.action).toBe('promote'))

    rerender({ projectId: projectB })
    await waitFor(() => expect(result.current.state).toBe('ready'))
    expect(result.current.candidates[0]?.card_id).toBe(`card-${projectB}`)
    expect(result.current.cards[0]?.card_id).toBe(`card-${projectB}`)

    await act(async () => {
      stalePromotion.resolve(cardFor(projectA))
      await promotionPromise
    })

    expect(result.current.candidates[0]?.card_id).toBe(`card-${projectB}`)
    expect(result.current.cards[0]?.card_id).toBe(`card-${projectB}`)
    expect(result.current.scope?.project_binding_id).toBe(projectB)
    expect(result.current.scopeFingerprint).toBe('4'.repeat(64))
  })
})

import { describe, expect, it, vi } from 'vitest'
import {
  currentProjectKbBinding,
  finalizeKnowledgeSourceImport,
  finalizeProjectKnowledgeSourceImport,
  getProjectKbState,
  getProjectKnowledgeScope,
  getKnowledgeSourceFormalization,
  importKnowledgeCandidates,
  importProjectKnowledgeCandidates,
  listProjectKnowledgeCandidates,
  listProjectKnowledgeCards,
  promoteKnowledgeCandidate,
  rejectKnowledgeCandidate,
  rebuildProjectKbState,
  rollbackKnowledgeCard,
  updateKnowledgeCandidate,
} from './knowledge'
import {
  currentProjectKbStateFixture,
  knowledgeCandidateFixture,
  knowledgeCandidateListFixture,
  knowledgeCardFixture,
  knowledgeFormalizationFixture,
  knowledgeFormalizationReceiptFixture,
  reviewedWorkCardFixture,
} from '../test/knowledgeFixture'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('knowledge API', () => {
  it('accepts only a complete current project KB pointer and matching Manifest', () => {
    const state = currentProjectKbStateFixture()
    const expectation = {
      projectId: state.project_id,
      projectRevision: state.manifest?.project.revision,
      projectFingerprint: state.manifest?.project.fingerprint,
    }

    expect(currentProjectKbBinding(state, expectation)).toEqual({
      project_kb_state_id: state.manifest?.state_id,
      project_kb_manifest_hash: state.manifest?.manifest_hash,
    })
    expect(currentProjectKbBinding({
      ...state,
      pointer: state.pointer && { ...state.pointer, manifest_hash: 'f'.repeat(64) },
    }, expectation)).toBeNull()
    expect(currentProjectKbBinding({ ...state, status: 'stale' }, expectation)).toBeNull()
  })

  it('imports candidates through a fixed source receipt endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      schema: 'zhimeng.knowledge-candidate-list.v2',
      candidates: [knowledgeCandidateFixture],
      candidate_set_hash: knowledgeCandidateListFixture.candidate_set_hash,
    }))

    await importKnowledgeCandidates(knowledgeCandidateFixture.source_import_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })

    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/knowledge/source-imports/${knowledgeCandidateFixture.source_import_id}/candidates`,
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBe('POST')
  })

  it('binds edits and promotion to candidate revision and content hash', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(knowledgeCandidateFixture))
      .mockResolvedValueOnce(jsonResponse(knowledgeCardFixture))

    await updateKnowledgeCandidate(knowledgeCandidateFixture, reviewedWorkCardFixture, { fetcher })
    await promoteKnowledgeCandidate(knowledgeCandidateFixture, true, { fetcher })

    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      expected_revision: knowledgeCandidateFixture.revision,
      expected_content_hash: knowledgeCandidateFixture.content_hash,
      card: reviewedWorkCardFixture,
    })
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_revision: knowledgeCandidateFixture.revision,
      expected_content_hash: knowledgeCandidateFixture.content_hash,
      replace_active: true,
    })
  })

  it('binds rollback to the current card state and explicit target version', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(knowledgeCardFixture))
    await rollbackKnowledgeCard(knowledgeCardFixture, knowledgeCardFixture.active_version_id, { fetcher })
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      expected_state_revision: knowledgeCardFixture.state_revision,
      expected_active_version_id: knowledgeCardFixture.active_version_id,
      target_version_id: knowledgeCardFixture.active_version_id,
    })
  })

  it('binds rejection and batch finalization to immutable review state', async () => {
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(knowledgeCandidateFixture))
      .mockResolvedValueOnce(jsonResponse(knowledgeFormalizationFixture))
      .mockResolvedValueOnce(jsonResponse(knowledgeFormalizationReceiptFixture))
    const options = { fetcher, baseUrl: 'http://127.0.0.1:8787' }
    const sourceImportId = knowledgeCandidateFixture.source_import_id

    await rejectKnowledgeCandidate(knowledgeCandidateFixture, options)
    await getKnowledgeSourceFormalization(sourceImportId, options)
    await finalizeKnowledgeSourceImport(sourceImportId, {
      expected_source_receipt_hash: '1'.repeat(64),
      expected_draft_hash: '2'.repeat(64),
      expected_candidate_set_hash: '3'.repeat(64),
    }, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    }))).toEqual([
      {
        url: `http://127.0.0.1:8787/api/v1/knowledge/candidates/${knowledgeCandidateFixture.candidate_id}/reject`,
        method: 'POST',
        body: {
          expected_revision: knowledgeCandidateFixture.revision,
          expected_content_hash: knowledgeCandidateFixture.content_hash,
        },
      },
      {
        url: `http://127.0.0.1:8787/api/v1/knowledge/source-imports/${sourceImportId}/formalization`,
        method: 'GET',
        body: undefined,
      },
      {
        url: `http://127.0.0.1:8787/api/v1/knowledge/source-imports/${sourceImportId}/finalize`,
        method: 'POST',
        body: {
          expected_source_receipt_hash: '1'.repeat(64),
          expected_draft_hash: '2'.repeat(64),
          expected_candidate_set_hash: '3'.repeat(64),
        },
      },
    ])
  })

  it('uses the project knowledge namespace and preserves scope metadata', async () => {
    const projectId = 'project/fiction-alpha'
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.knowledge-scope-record.v1',
        scope_id: 'knowledge-scope-project-test',
        scope_type: 'project',
        project_binding_id: projectId,
        state_revision: 1,
        created_at: '2026-07-21T05:00:00Z',
        updated_at: '2026-07-21T05:00:00Z',
      }))
      .mockResolvedValueOnce(jsonResponse(knowledgeCandidateListFixture))
      .mockResolvedValueOnce(jsonResponse(knowledgeCandidateListFixture))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.knowledge-scoped-card-list.v1',
        scope: {
          schema: 'zhimeng.knowledge-scope-record.v1',
          scope_id: 'knowledge-scope-project-test',
          scope_type: 'project',
          project_binding_id: projectId,
          state_revision: 1,
          created_at: '2026-07-21T05:00:00Z',
          updated_at: '2026-07-21T05:00:00Z',
        },
        includes_application_scope: true,
        cards: [],
        inherited_card_ids: [],
        fingerprint: '9'.repeat(64),
      }))
      .mockResolvedValueOnce(jsonResponse(knowledgeFormalizationReceiptFixture))

    const options = { fetcher, baseUrl: 'http://127.0.0.1:8787' }
    const sourceId = knowledgeCandidateFixture.source_import_id
    await getProjectKnowledgeScope(projectId, options)
    await importProjectKnowledgeCandidates(projectId, sourceId, options)
    await listProjectKnowledgeCandidates(projectId, sourceId, options)
    const cards = await listProjectKnowledgeCards(projectId, options)
    await finalizeProjectKnowledgeSourceImport(projectId, sourceId, {
      expected_source_receipt_hash: '1'.repeat(64),
      expected_draft_hash: '2'.repeat(64),
      expected_candidate_set_hash: '3'.repeat(64),
    }, options)

    expect(cards.schema).toBe('zhimeng.knowledge-scoped-card-list.v1')
    expect(cards.includes_application_scope).toBe(true)
    expect(cards.inherited_card_ids).toEqual([])
    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
    }))).toEqual([
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/scope`,
        method: 'GET',
      },
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/source-imports/${sourceId}/candidates`,
        method: 'POST',
      },
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/candidates?source_import_id=${sourceId}`,
        method: 'GET',
      },
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/cards`,
        method: 'GET',
      },
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${encodeURIComponent(projectId)}/knowledge/source-imports/${sourceId}/formalization/finalize`,
        method: 'POST',
      },
    ])
  })

  it('reads and rebuilds the derived project KB with complete CAS inputs', async () => {
    const projectId = `project-${'a'.repeat(24)}`
    const state = {
      schema: 'zhimeng.project-kb-state-record.v1',
      project_id: projectId,
      status: 'missing',
      runtime_relative_root: `project-kb/${projectId}.kb`,
      pointer: null,
      manifest: null,
      reasons: [],
    }
    const fetcher = vi.fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(state))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.project-kb-rebuild-result.v1',
        reused_snapshot: false,
        record: { ...state, status: 'current' },
      }))
    const options = { fetcher, baseUrl: 'http://127.0.0.1:8787' }
    const request = {
      expected_project_revision: 7,
      expected_project_fingerprint: '1'.repeat(64),
      expected_scope_state_revision: 3,
      expected_knowledge_fingerprint: '2'.repeat(64),
    }

    await getProjectKbState(projectId, options)
    await rebuildProjectKbState(projectId, request, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body ? JSON.parse(String(init.body)) : undefined,
    }))).toEqual([
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${projectId}/knowledge/kb-state`,
        method: 'GET',
        body: undefined,
      },
      {
        url: `http://127.0.0.1:8787/api/v1/projects/${projectId}/knowledge/kb-state/rebuild`,
        method: 'POST',
        body: request,
      },
    ])
  })
})

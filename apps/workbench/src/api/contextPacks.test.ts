import { describe, expect, it, vi } from 'vitest'
import {
  CONTEXT_PACK_COMPILE_TIMEOUT_MS,
  createContextPack,
  getContextPack,
  getNlclRoute,
  getNlclRouteExecution,
  getPrewriteOperationPackage,
  getRunContextPack,
  listSessionContextPacks,
  projectKbContractBindingStatus,
  type ContextCreateRequest,
  type ContextPackRecord,
} from './contextPacks'
import {
  nlclRouteExecutionFixture,
  nlclRouteExecutionReceiptFixture,
  nlclRouteFixture,
  prewriteOperationPackageFixture,
} from '../test/nlclRouteFixture'
import { contextPackFixture } from '../test/contextPackFixture'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const projectKbStateId = `project-kb-state-${'6'.repeat(32)}`
const projectKbManifestHash = '7'.repeat(64)

const record: ContextPackRecord = {
  schema: 'zhimeng.context-pack-record.v1',
  pack: {
    schema: 'zhimeng.context-pack.v1',
    context_pack_id: 'context-pack-one',
    session_id: 'session-one',
    project_id: 'project-one',
    history_through_ordinal: 4,
    history_message_count: 2,
    objective: 'Continue the chapter.',
    profile: 'chapter',
    active_chapter_id: 'CH001',
    project_revision: 7,
    project_fingerprint: 'a'.repeat(64),
    knowledge_fingerprint: 'b'.repeat(64),
    project_kb_state_id: projectKbStateId,
    project_kb_manifest_hash: projectKbManifestHash,
    compiler_version: 'context-compiler.v1',
    policy_version: 'context-selection.v1',
    budget: {
      character_estimator: 'unicode_codepoints_v1',
      token_estimator: 'conservative_utf8_bytes_div3_v1',
      max_characters: 20_000,
      used_characters: 2_000,
      remaining_characters: 18_000,
      overflow_characters: 0,
      max_tokens: 8_000,
      used_tokens: 900,
      remaining_tokens: 7_100,
      overflow_tokens: 0,
    },
    included: [{
      source_id: 'project-chapter-one',
      source_type: 'chapter',
      title: 'Chapter one',
      authority: 'project_truth',
      version: 'revision-7',
      source_hash: 'c'.repeat(64),
      content: 'Approved project truth.',
      priority: 100,
      mandatory: true,
      fragment_hash: 'd'.repeat(64),
      estimated_characters: 400,
      estimated_tokens: 150,
      selection_reason: 'mandatory',
    }],
    excluded: [{
      source_id: 'knowledge-risk-one',
      source_type: 'knowledge_card',
      title: 'Risk reference',
      authority: 'risk',
      version: 'candidate-v1',
      source_hash: 'e'.repeat(64),
      priority: 90,
      mandatory: false,
      eligible: true,
      candidate_exclusion_reason: null,
      fragment_hash: 'f'.repeat(64),
      estimated_characters: 300,
      estimated_tokens: 120,
      exclusion_reason: 'risk_authority',
    }],
    gates: [{
      code: 'risk_fragment_excluded',
      severity: 'warning',
      message: 'Risk reference excluded.',
      source_id: 'knowledge-risk-one',
    }],
    exact_provider_messages: [
      { role: 'system', content: 'Exact system context.' },
      { role: 'user', content: 'Earlier request.' },
      { role: 'assistant', content: 'Earlier completed response.' },
      { role: 'user', content: 'Continue the chapter.' },
    ],
    pack_hash: '1'.repeat(64),
    injection_hash: '2'.repeat(64),
    provider_messages_hash: '3'.repeat(64),
    readiness: 'degraded',
    created_at: '2026-07-20T00:00:00Z',
  },
  consumed_by_run_id: 'run-one',
}

describe('context pack API client', () => {
  it('distinguishes a chain-consistent KB binding from legacy and mismatched records', () => {
    const modernRecord = contextPackFixture()
    const modernRoute = nlclRouteFixture()
    const receipt = nlclRouteExecutionReceiptFixture(modernRoute)
    expect(projectKbContractBindingStatus(modernRecord, modernRoute, [
      nlclRouteExecutionFixture(modernRoute, { status: 'succeeded', receipt }),
    ])).toBe('current')

    const legacyRecord = contextPackFixture({
      project_kb_state_id: null,
      project_kb_manifest_hash: null,
    })
    const legacyRoute = nlclRouteFixture({
      project_kb_state_id: null,
      project_kb_manifest_hash: null,
    })
    expect(projectKbContractBindingStatus(legacyRecord, legacyRoute)).toBe('legacy')
    expect(projectKbContractBindingStatus(modernRecord, nlclRouteFixture({
      project_kb_manifest_hash: 'f'.repeat(64),
    }))).toBe('mismatch')
  })

  it('uses fixed encoded routes and parses the session list wrapper', async () => {
    const routeFixture = nlclRouteFixture()
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(record))
      .mockResolvedValueOnce(jsonResponse(record))
      .mockResolvedValueOnce(jsonResponse(routeFixture))
      .mockResolvedValueOnce(jsonResponse(nlclRouteExecutionFixture(routeFixture)))
      .mockResolvedValueOnce(jsonResponse(prewriteOperationPackageFixture(routeFixture)))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.context-pack-list.v1',
        session_id: 'session/a b?',
        context_packs: [record],
      }))
      .mockResolvedValueOnce(jsonResponse(record))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const request: ContextCreateRequest = {
      objective: 'Continue the chapter.',
      profile: 'chapter',
      active_chapter_id: 'CH001',
      expected_project_revision: 7,
      expected_project_fingerprint: 'a'.repeat(64),
      project_kb_state_id: projectKbStateId,
      project_kb_manifest_hash: projectKbManifestHash,
    }

    await createContextPack('session/a b?', request, 'context-create-key', options)
    await getContextPack('context/a b?', options)
    const route = await getNlclRoute('context/a b?', options)
    const routeExecution = await getNlclRouteExecution('context/a b?', 1, options)
    const operationPackage = await getPrewriteOperationPackage('context/a b?', options)
    const listed = await listSessionContextPacks('session/a b?', options)
    await getRunContextPack('run/a b?', options)

    expect(listed).toEqual([record])
    expect(route.schema).toBe('zhimeng.nlcl-route-receipt.v1')
    expect(routeExecution.status).toBe('awaiting_execution')
    expect(operationPackage.readiness).toBe('blocked')
    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/sessions/session%2Fa%20b%3F/context-packs',
        method: 'POST',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/context-packs/context%2Fa%20b%3F',
        method: 'GET',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/context-packs/context%2Fa%20b%3F/nlcl-route',
        method: 'GET',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/context-packs/context%2Fa%20b%3F/nlcl-route/executions/1',
        method: 'GET',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/context-packs/context%2Fa%20b%3F/prewrite-operation-package',
        method: 'GET',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/sessions/session%2Fa%20b%3F/context-packs?limit=100',
        method: 'GET',
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/agent/runs/run%2Fa%20b%3F/context-pack',
        method: 'GET',
      },
    ])
  })

  it('sends the idempotency header and only the public create request', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(record))
    const request: ContextCreateRequest = {
      objective: 'Rewrite the scene.',
      profile: 'rewrite',
      active_chapter_id: null,
      expected_project_revision: 9,
      expected_project_fingerprint: '9'.repeat(64),
      project_kb_state_id: projectKbStateId,
      project_kb_manifest_hash: projectKbManifestHash,
    }

    await createContextPack(
      'session-one',
      request,
      'context-create-key',
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    const init = fetcher.mock.calls[0]?.[1]
    const headers = new Headers(init?.headers)
    expect(headers.get('Content-Type')).toBe('application/json')
    expect(headers.get('Idempotency-Key')).toBe('context-create-key')
    expect(JSON.parse(String(init?.body))).toEqual(request)
    expect(Object.keys(JSON.parse(String(init?.body))).sort()).toEqual([
      'active_chapter_id',
      'expected_project_fingerprint',
      'expected_project_revision',
      'objective',
      'profile',
      'project_kb_manifest_hash',
      'project_kb_state_id',
    ])
  })

  it('rejects an empty successful NLCL execution response', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(null))

    await expect(getNlclRouteExecution(
      'context-one',
      2,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toThrow('无效的 NLCL 执行状态')
  })

  it('keeps Context compilation alive beyond the generic request timeout', async () => {
    vi.useFakeTimers()
    try {
      let requestSignal: AbortSignal | null | undefined
      const fetcher = vi.fn<typeof fetch>((_input, init) => new Promise<Response>((_resolve, reject) => {
        requestSignal = init?.signal
        requestSignal?.addEventListener('abort', () => reject(new Error('aborted')))
      }))
      const pending = createContextPack(
        'session-one',
        {
          objective: 'Compile a large project context.',
          profile: 'chapter',
          active_chapter_id: 'CH001',
          expected_project_revision: 1,
          expected_project_fingerprint: 'a'.repeat(64),
          project_kb_state_id: projectKbStateId,
          project_kb_manifest_hash: projectKbManifestHash,
        },
        'context-long-running',
        { fetcher },
      ).catch((error: unknown) => error)

      await vi.advanceTimersByTimeAsync(3_501)
      expect(requestSignal?.aborted).toBe(false)

      await vi.advanceTimersByTimeAsync(CONTEXT_PACK_COMPILE_TIMEOUT_MS - 3_501)
      expect(requestSignal?.aborted).toBe(true)
      expect(await pending).toBeInstanceOf(Error)
    } finally {
      vi.useRealTimers()
    }
  })
})

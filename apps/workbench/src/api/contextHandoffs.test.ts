import { describe, expect, it, vi } from 'vitest'
import {
  approveContextHandoff,
  disableContextHandoff,
  getContextHandoff,
  prepareContextHandoff,
  rejectContextHandoff,
  runContextHandoff,
} from './contextHandoffs'
import { contextHandoffResponseFixture } from '../test/contextHandoffFixture'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('context handoff API client', () => {
  it('uses project-scoped O2-E routes and CAS request bodies', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse(contextHandoffResponseFixture()),
    )
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const projectId = 'project/a b?'
    const contextHandoffId = `context-handoff-${'a'.repeat(32)}`

    await getContextHandoff(projectId, options)
    await prepareContextHandoff(projectId, {
      expected_project_revision: 9,
      expected_project_fingerprint: 'f'.repeat(64),
      profile: 'chapter',
      stage: 'draft',
      chapter_id: 'CH003',
      task_intent: '生成写前 Context Pack，不写正文。',
    }, options)
    await approveContextHandoff(projectId, contextHandoffId, options)
    await rejectContextHandoff(projectId, contextHandoffId, options)
    await disableContextHandoff(projectId, contextHandoffId, options)
    await runContextHandoff(projectId, contextHandoffId, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble/prepare',
        method: 'POST',
        body: {
          expected_project_revision: 9,
          expected_project_fingerprint: 'f'.repeat(64),
          profile: 'chapter',
          stage: 'draft',
          chapter_id: 'CH003',
          task_intent: '生成写前 Context Pack，不写正文。',
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble/approve',
        method: 'POST',
        body: { expected_context_handoff_id: contextHandoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble/reject',
        method: 'POST',
        body: { expected_context_handoff_id: contextHandoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble/disable',
        method: 'POST',
        body: { expected_context_handoff_id: contextHandoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/context-handoffs/novel-kb-assemble/run',
        method: 'POST',
        body: { expected_context_handoff_id: contextHandoffId },
      },
    ])
  })
})

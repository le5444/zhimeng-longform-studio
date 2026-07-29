import { describe, expect, it, vi } from 'vitest'
import {
  approveSkillHandoff,
  disableSkillHandoff,
  getSkillHandoff,
  prepareSkillHandoff,
  rejectSkillHandoff,
  runSkillHandoff,
} from './skillHandoffs'
import { skillHandoffResponseFixture } from '../test/skillHandoffFixture'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('skill handoff API client', () => {
  it('uses project-scoped O2-D routes and CAS request bodies', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValue(jsonResponse(skillHandoffResponseFixture()))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const projectId = 'project/a b?'
    const handoffId = `skill-handoff-${'a'.repeat(32)}`

    await getSkillHandoff(projectId, options)
    await prepareSkillHandoff(projectId, {
      expected_project_revision: 9,
      expected_project_fingerprint: 'f'.repeat(64),
      source_import_id: `source-import-${'d'.repeat(32)}`,
    }, options)
    await approveSkillHandoff(projectId, handoffId, options)
    await rejectSkillHandoff(projectId, handoffId, options)
    await disableSkillHandoff(projectId, handoffId, options)
    await runSkillHandoff(projectId, handoffId, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb/prepare',
        method: 'POST',
        body: {
          expected_project_revision: 9,
          expected_project_fingerprint: 'f'.repeat(64),
          source_import_id: `source-import-${'d'.repeat(32)}`,
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb/approve',
        method: 'POST',
        body: { expected_handoff_id: handoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb/reject',
        method: 'POST',
        body: { expected_handoff_id: handoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb/disable',
        method: 'POST',
        body: { expected_handoff_id: handoffId },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/skill-handoffs/novel-distillation-kb/run',
        method: 'POST',
        body: { expected_handoff_id: handoffId },
      },
    ])
  })

  it('extends timeout for adapter execution', async () => {
    let timeoutSignal: AbortSignal | undefined
    const fetcher = vi.fn<typeof fetch>().mockImplementation((_url, init) => {
      timeoutSignal = init?.signal as AbortSignal
      return Promise.resolve(jsonResponse(skillHandoffResponseFixture({ status: 'succeeded' })))
    })

    await runSkillHandoff('project-o2d', `skill-handoff-${'a'.repeat(32)}`, { fetcher, timeoutMs: 1 })

    expect(timeoutSignal).toBeDefined()
  })
})

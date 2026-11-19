import { describe, expect, it, vi } from 'vitest'
import { CHANGE_SET_WRITEBACK_TIMEOUT_MS, applyChangeSet, applyChangeSetDirect, createProjectChangeSet, getChangeSet, listProjectChangeSets, rollbackChangeSet, updateHunkDecisions } from './changeSets'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('change set API client', () => {
  it('lists project change sets and loads selected detail through stable IDs', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.change-set-list.v1',
        change_sets: [{ change_set_id: 'changeset-first' }],
      }))
      .mockResolvedValueOnce(jsonResponse({ change_set_id: 'changeset-first' }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    const records = await listProjectChangeSets('project-one', options)
    await getChangeSet('changeset-first', options)

    expect(records[0].change_set_id).toBe('changeset-first')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      'http://127.0.0.1:8787/api/v1/projects/project-one/change-sets',
      'http://127.0.0.1:8787/api/v1/change-sets/changeset-first',
    ])
  })

  it('sends decisions and writeback commands with the current review revision', async () => {
    const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => jsonResponse({}))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    await updateHunkDecisions(
      'changeset-one',
      4,
      [{ hunk_id: 'hunk-one', decision: 'accepted' }],
      options,
    )
    await applyChangeSet('changeset-one', 5, options)
    await applyChangeSetDirect('changeset-one', 5, options)
    await rollbackChangeSet('changeset-one', 6, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method,
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/decisions',
        method: 'PATCH',
        body: {
          expected_review_revision: 4,
          decisions: [{ hunk_id: 'hunk-one', decision: 'accepted' }],
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/apply',
        method: 'POST',
        body: { expected_review_revision: 5 },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/apply-direct',
        method: 'POST',
        body: { expected_review_revision: 5 },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/change-sets/changeset-one/rollback',
        method: 'POST',
        body: { expected_review_revision: 6 },
      },
    ])
    expect(CHANGE_SET_WRITEBACK_TIMEOUT_MS).toBeGreaterThan(3_500)
  })

  it('creates a manual author change set against the project writeback endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      change_set_id: 'changeset-created',
      status: 'reviewing',
    }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    await createProjectChangeSet('project-one', {
      base_revision: 3,
      base_fingerprint: 'a'.repeat(64),
      title: '作者修改 CH001',
      summary: '作者手改正文',
      origin: 'manual',
      changes: [{
        operation: 'update',
        file_id: 'file-0123456789abcdef0123456789abcdef',
        after_content: '新的正文\n',
        rationale: '作者手改正文。',
      }],
    }, options)

    expect(fetcher).toHaveBeenCalledTimes(1)
    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8787/api/v1/projects/project-one/change-sets')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toMatchObject({
      schema: 'zhimeng.change-set-create.v1',
      origin: 'manual',
      gate_required: false,
      title: '作者修改 CH001',
      changes: [{ operation: 'update' }],
    })
  })

})

import { describe, expect, it, vi } from 'vitest'
import { getProjectAssets, rebuildProjectAssets } from './projectAssets'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

const indexFixture = {
  schema: 'zhimeng.project-asset-index.v1' as const,
  project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
  project_root: 'C:/tmp/book',
  asset_count: 1,
  assets: [
    {
      asset_id: 'asset-' + 'b'.repeat(24),
      kind: 'character',
      title: '青崖',
      relative_path: '03_人物设定/主角.md',
      content_hash: 'c'.repeat(64),
      byte_size: 32,
      relationship_count: 0,
      status_tracked: false,
    },
  ],
  relationship_graph_complete: false as const,
  status_tracking_complete: false as const,
  formal_draft_allowed: false as const,
  policy_version: 'local-v1-project-assets-1',
  generated_at: '2026-07-26T00:00:00+00:00',
  index_id: 'project-asset-index-' + 'd'.repeat(32),
  index_hash: 'e'.repeat(64),
}

const responseFixture = {
  schema: 'zhimeng.project-asset-index-response.v1' as const,
  index: indexFixture,
  not_claiming: [
    'relationship_graph_complete',
    'status_tracking_complete',
    'formal_draft_allowed',
  ],
}

describe('projectAssets API', () => {
  it('rebuilds project asset index', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(responseFixture))
    const result = await rebuildProjectAssets(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.index.asset_count).toBe(1)
    expect(result.index.relationship_graph_complete).toBe(false)
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/assets/rebuild',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('gets existing project asset index', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(responseFixture))
    const result = await getProjectAssets(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.index.assets[0]?.title).toBe('青崖')
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/assets',
      expect.objectContaining({ method: 'GET' }),
    )
  })
})

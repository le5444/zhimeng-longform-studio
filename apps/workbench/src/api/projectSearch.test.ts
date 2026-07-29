import { describe, expect, it, vi } from 'vitest'
import {
  PROJECT_SEARCH_TIMEOUT_MS,
  rebuildProjectSearchIndex,
  searchProject,
} from './projectSearch'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const rebuildFixture = {
  schema: 'zhimeng.project-search-rebuild-result.v1',
  project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
  rebuilt: true,
  status: 'rebuilt',
  manifest: {
    schema: 'zhimeng.project-search-index.v1',
    project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
    builder_id: 'zhimeng.project-search',
    builder_version: '1',
    source_fingerprint: 'a'.repeat(64),
    project_revision: 2,
    project_fingerprint: 'b'.repeat(64),
    knowledge_fingerprint: 'c'.repeat(64),
    document_count: 3,
    full_text_indexed: true,
    vector_index_ready: false,
    rebuilt_at: '2026-07-26T00:00:00+00:00',
    index_hash: 'd'.repeat(64),
  },
}

const searchFixture = {
  schema: 'zhimeng.project-search-query-response.v1',
  project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
  query: '阶段兑现',
  index_hash: 'd'.repeat(64),
  source_fingerprint: 'a'.repeat(64),
  rebuilt_before_search: false,
  match_mode: 'full_text',
  vector_enhanced: false,
  hit_count: 1,
  hits: [
    {
      schema: 'zhimeng.project-search-hit.v1',
      source_id: 'file-' + 'e'.repeat(32),
      source_type: 'project_file',
      authority: 'project_truth',
      title: '05_正文内容/第一卷文件夹/CH001.md',
      relative_path: '05_正文内容/第一卷文件夹/CH001.md',
      version: 'rev-2',
      source_hash: 'f'.repeat(64),
      snippet: '青崖完成阶段兑现',
      reason: 'content_match',
      score: 8.25,
      updated_at: null,
    },
  ],
}

describe('projectSearch API', () => {
  it('uses a generous rebuild/search timeout', () => {
    expect(PROJECT_SEARCH_TIMEOUT_MS).toBe(60_000)
  })

  it('rebuilds the project search index', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(rebuildFixture))
    const result = await rebuildProjectSearchIndex(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      { force: true },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.rebuilt).toBe(true)
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:8787/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/search/rebuild',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      schema: 'zhimeng.project-search-rebuild-request.v1',
      force: true,
    })
  })

  it('searches with query and filters', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(searchFixture))
    const result = await searchProject(
      'project-aaaaaaaaaaaaaaaaaaaaaaaa',
      {
        query: '阶段兑现',
        source_types: ['project_file'],
        authorities: ['project_truth'],
        limit: 10,
      },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.hit_count).toBe(1)
    expect(result.hits[0]?.snippet).toContain('阶段兑现')
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      schema: 'zhimeng.project-search-query-request.v1',
      query: '阶段兑现',
      source_types: ['project_file'],
      authorities: ['project_truth'],
      limit: 10,
      auto_rebuild: true,
    })
  })
})

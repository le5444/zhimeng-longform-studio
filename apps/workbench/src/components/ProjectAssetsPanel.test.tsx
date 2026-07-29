import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { ProjectAssetsPanel } from './ProjectAssetsPanel'

const assetsFixture = {
  schema: 'zhimeng.project-asset-index-response.v1',
  index: {
    schema: 'zhimeng.project-asset-index.v1',
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
    relationship_graph_complete: false,
    status_tracking_complete: false,
    formal_draft_allowed: false,
    policy_version: 'local-v1-project-assets-1',
    generated_at: '2026-07-26T00:00:00+00:00',
    index_id: 'project-asset-index-' + 'd'.repeat(32),
    index_hash: 'e'.repeat(64),
  },
  not_claiming: [
    'relationship_graph_complete',
    'status_tracking_complete',
    'formal_draft_allowed',
  ],
}

describe('ProjectAssetsPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('loads assets for a bound project and can rebuild', async () => {
    const user = userEvent.setup()
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response(JSON.stringify(assetsFixture), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    render(
      <ProjectAssetsPanel
        enabled
        projectId="project-aaaaaaaaaaaaaaaaaaaaaaaa"
      />,
    )

    await waitFor(() => {
      expect(screen.getByText('青崖')).toBeInTheDocument()
    })
    expect(screen.getByText(/人物 · 03_人物设定\/主角.md/)).toBeInTheDocument()
    expect(screen.getByText(/关系图未完成/)).toBeInTheDocument()
    expect(vi.mocked(globalThis.fetch).mock.calls[0]?.[0]).toContain(
      '/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/assets',
    )

    await user.click(screen.getByRole('button', { name: '重建' }))
    await waitFor(() => {
      expect(vi.mocked(globalThis.fetch).mock.calls.some((call) =>
        String(call[0]).includes('/assets/rebuild'),
      )).toBe(true)
    })
  })

  it('blocks assets when runtime is unavailable', () => {
    render(<ProjectAssetsPanel enabled={false} projectId={null} />)
    expect(screen.getByText('Writing Runtime 未就绪')).toBeInTheDocument()
  })

  it('asks for project binding when project is missing', () => {
    render(<ProjectAssetsPanel enabled projectId={null} />)
    expect(screen.getByText('请先绑定小说项目')).toBeInTheDocument()
  })
})

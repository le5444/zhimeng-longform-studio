import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest'
import { ProjectSearchPanel } from './ProjectSearchPanel'

const searchFixture = {
  schema: 'zhimeng.project-search-query-response.v1',
  project_id: 'project-aaaaaaaaaaaaaaaaaaaaaaaa',
  query: '阶段兑现',
  index_hash: 'd'.repeat(64),
  source_fingerprint: 'a'.repeat(64),
  rebuilt_before_search: true,
  match_mode: 'full_text' as const,
  vector_enhanced: false as const,
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

describe('ProjectSearchPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('searches the bound project and surfaces hits', async () => {
    const user = userEvent.setup()
    const onOpen = vi.fn()
    vi.mocked(globalThis.fetch).mockResolvedValue(
      new Response(JSON.stringify(searchFixture), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }),
    )

    render(
      <ProjectSearchPanel
        enabled
        projectId="project-aaaaaaaaaaaaaaaaaaaaaaaa"
        onOpenProjectFile={onOpen}
      />,
    )

    await user.type(screen.getByLabelText('检索词'), '阶段兑现')
    await user.click(screen.getByRole('button', { name: '检索' }))

    await waitFor(() => {
      expect(screen.getByText('青崖完成阶段兑现')).toBeInTheDocument()
    })
    expect(screen.getByText(/项目文件/)).toBeInTheDocument()
    expect(vi.mocked(globalThis.fetch).mock.calls[0]?.[0]).toContain(
      '/api/v1/projects/project-aaaaaaaaaaaaaaaaaaaaaaaa/search',
    )

    await user.click(screen.getByRole('button', { name: '打开' }))
    expect(onOpen).toHaveBeenCalledWith(
      searchFixture.hits[0].source_id,
      searchFixture.hits[0].relative_path,
    )
  })

  it('blocks search when runtime is unavailable', () => {
    render(<ProjectSearchPanel enabled={false} projectId={null} />)
    expect(screen.getByText('Writing Runtime 未就绪')).toBeInTheDocument()
  })
})

import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { App } from './App'
import { candidateBoundChangeSet } from './lib/candidateChangeSet'
import { agentCandidateOutputFixture } from './test/agentCandidateOutputFixture'
import { changeSetFixture } from './test/changeSetFixture'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('author workbench shell', () => {
  afterEach(() => {
    cleanup()
    window.localStorage.clear()
    vi.unstubAllGlobals()
  })

  it('keeps a remembered project on the bookshelf while Runtime is offline', async () => {
    window.localStorage.setItem(
      'zhimeng.workbench.selectedProjectId',
      'project-0123456789abcdef01234567',
    )
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('offline')))
    render(<App />)

    expect(screen.getByLabelText('书架')).toBeInTheDocument()
    expect(screen.queryByLabelText('章节正文编辑器')).not.toBeInTheDocument()
    expect(screen.queryByText('雾港来信（示例）')).not.toBeInTheDocument()
    expect(await screen.findAllByText('Runtime 离线')).not.toHaveLength(0)
  })

  it('replaces the example tree and manuscript with a bound project snapshot', async () => {
    const projectId = 'project-0123456789abcdef01234567'
    const fileId = 'file-0123456789abcdef0123456789abcdef'
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const url = String(input)
        if (url.endsWith('/health/ready')) {
          return Promise.resolve(jsonResponse({ status: 'ready', version: '0.1.0' }))
        }
        if (url.endsWith('/api/v1/runtime/status')) {
          return Promise.resolve(jsonResponse({ state: 'ready', version: '0.1.0' }))
        }
        if (url.endsWith('/api/v1/projects')) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.project-bindings.v1',
              projects: [{
                project_id: projectId,
                display_name: '真实测试作品',
                root_path: 'C:\\Novels\\真实测试作品',
                revision: 3,
                fingerprint: 'a'.repeat(64),
                snapshot_ready: true,
                created_at: '2026-07-27T00:00:00Z',
                updated_at: '2026-07-27T00:00:00Z',
              }],
            }),
          )
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/inspect`)) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.project-inspection.v1',
              read_only: true,
              project: {
                project_id: projectId,
                display_name: '真实测试作品',
                root_path: 'C:\\Novels\\真实测试作品',
                format: '01-09',
                revision: 3,
                fingerprint: 'a'.repeat(64),
              },
              sections: [],
              missing_sections: [],
              volumes: [{ volume_id: 'VOL001', number: 1, chapter_ids: ['VOL001-CH001'] }],
              chapters: [{
                chapter_id: 'VOL001-CH001',
                number: 1,
                volume_id: 'VOL001',
                file_ids: [fileId],
                prose_file_ids: [fileId],
              }],
              files: [{
                file_id: fileId,
                relative_path: '05_正文内容/第一卷/CH001.md',
                absolute_path: 'C:\\Novels\\真实测试作品\\05_正文内容\\第一卷\\CH001.md',
                section_index: 5,
                chapter_id: 'VOL001-CH001',
                volume_id: 'VOL001',
                kind: 'prose',
                size_bytes: 18,
                sha256: 'b'.repeat(64),
              }],
              warnings: [],
            }),
          )
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/files/${fileId}`)) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.project-file-content.v1',
              read_only: true,
              project_id: projectId,
              snapshot_revision: 3,
              project_fingerprint: 'a'.repeat(64),
              file: {},
              etag: 'b'.repeat(64),
              content: '# 第一章\n\n这是真实项目正文。',
            }),
          )
        }
        if (url.endsWith('/api/v1/agent/status')) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.agent-status.v1',
              available: false,
              detail: 'Writing Agent provider is not configured',
              executor: null,
              capabilities: null,
            }),
          )
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.agent-session-list.v1',
              sessions: [],
            }),
          )
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/change-sets`)) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.change-set-list.v1',
              change_sets: [],
            }),
          )
        }
        if (url.endsWith('/api/v1/settings/provider')) {
          return Promise.resolve(
            jsonResponse({
              schema: 'zhimeng.provider-settings.v1',
              base_url: '',
              model: '',
              api_key_set: false,
              source: 'none',
              configured: false,
            }),
          )
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`))
      }),
    )

    render(<App />)

    const bookCard = await screen.findByRole('button', { name: /真实测试作品/ })
    fireEvent.click(bookCard)
    expect(await screen.findByText('这是真实项目正文。')).toBeInTheDocument()
    expect(screen.getByText('只读项目快照')).toBeInTheDocument()
    expect(screen.getByText('rev 3')).toBeInTheDocument()
    expect(screen.getByText('可编辑 · 修改先审阅')).toBeInTheDocument()
    expect(screen.queryByText('修改计划已暂存')).not.toBeInTheDocument()

    const configureModel = await screen.findByRole('button', { name: '先配置模型' })
    fireEvent.click(configureModel)
    expect(await screen.findByRole('dialog', { name: '写作模型设置' })).toBeInTheDocument()
  })

  it('shows an explicit read-only error instead of editable example data', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn<typeof fetch>((input) => {
        const url = String(input)
        if (url.endsWith('/health/ready')) {
          return Promise.resolve(jsonResponse({ status: 'ready', version: '0.1.0' }))
        }
        if (url.endsWith('/api/v1/runtime/status')) {
          return Promise.resolve(jsonResponse({ state: 'ready', version: '0.1.0' }))
        }
        if (url.endsWith('/api/v1/projects')) {
          return Promise.resolve(
            new Response(
              JSON.stringify({
                schema: 'zhimeng.error.v1',
                error: {
                  code: 'project_unavailable',
                  message: '绑定项目读取失败',
                  request_id: 'test-request',
                  retryable: true,
                },
              }),
              { status: 500, headers: { 'Content-Type': 'application/json' } },
            ),
          )
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`))
      }),
    )

    render(<App />)

    expect(await screen.findByRole('alert')).toHaveTextContent('绑定项目读取失败')
    expect(screen.queryByText('雾港来信（示例）')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('章节正文编辑器')).not.toBeInTheDocument()
  })

  it('never projects an unrelated ChangeSet beside an active Candidate', async () => {
    const candidate = await agentCandidateOutputFixture()
    const unrelated = changeSetFixture({ origin: 'agent' })
    const matching = {
      ...unrelated,
      source_run_id: candidate.run_id,
    }
    const otherProject = {
      ...matching,
      project_id: 'project-fedcba9876543210fedcba98',
    }

    expect(candidateBoundChangeSet(candidate, null, unrelated)).toBeNull()
    expect(candidateBoundChangeSet(candidate, unrelated.change_set_id, unrelated)).toBeNull()
    expect(candidateBoundChangeSet(candidate, 'changeset-other', matching)).toBeNull()
    expect(candidateBoundChangeSet(candidate, matching.change_set_id, otherProject)).toBeNull()
    expect(candidateBoundChangeSet(candidate, matching.change_set_id, matching)).toBe(matching)
    expect(candidateBoundChangeSet(null, null, unrelated)).toBe(unrelated)
  })
})

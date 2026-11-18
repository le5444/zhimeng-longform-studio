import { describe, expect, it, vi } from 'vitest'
import {
  inspectBoundProject,
  inspectProjectRoot,
  inspectProjectRootWithProgress,
  listProjectBindings,
  readBoundProjectFile,
} from './projects'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('project API client', () => {
  it('keeps the connection and token for every scan poll', async () => {
    vi.useFakeTimers()
    try {
      const result = { schema: 'zhimeng.project-inspection.v1' }
      const fetcher = vi.fn<typeof fetch>()
        .mockResolvedValueOnce(jsonResponse({ job_id: 'scan-1' }))
        .mockResolvedValueOnce(jsonResponse({ status: 'running', scanned: 1, total: 2 }))
        .mockResolvedValueOnce(jsonResponse({ status: 'done', scanned: 2, total: 2, result }))
      const progress = vi.fn()
      const pending = inspectProjectRootWithProgress('C:/ordinary-folder', progress, {
        baseUrl: 'https://scan.example', token: 'scan-token', fetcher,
      })
      await vi.advanceTimersByTimeAsync(800)
      expect(await pending).toEqual(result)
      expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
        'https://scan.example/api/v1/projects/inspect-jobs',
        'https://scan.example/api/v1/projects/inspect-jobs/scan-1',
        'https://scan.example/api/v1/projects/inspect-jobs/scan-1',
      ])
      for (const [, init] of fetcher.mock.calls) expect(new Headers(init?.headers).get('Authorization')).toBe('Bearer scan-token')
      expect(progress).toHaveBeenLastCalledWith({ scanned: 2, total: 2 })
    } finally { vi.useRealTimers() }
  })

  it.each(['after-create', 'during-delay', 'during-poll'])('cancels scanning %s', async (when) => {
    vi.useFakeTimers()
    try {
      const controller = new AbortController()
      const fetcher = vi.fn<typeof fetch>().mockImplementation(async (_url, init) => {
        if (fetcher.mock.calls.length === 1) {
          if (when === 'after-create') controller.abort()
          return jsonResponse({ job_id: 'scan-1' })
        }
        return new Promise<Response>((_resolve, reject) => init?.signal?.addEventListener('abort', () => reject(init.signal?.reason), { once: true }))
      })
      const progress = vi.fn()
      const pending = inspectProjectRootWithProgress('C:/ordinary-folder', progress, { fetcher, signal: controller.signal })
        .then(() => null, (error: unknown) => error)
      await vi.advanceTimersByTimeAsync(when === 'during-poll' ? 450 : 100)
      controller.abort()
      expect(await pending).toMatchObject({ name: 'AbortError' })
      await vi.advanceTimersByTimeAsync(1000)
      expect(fetcher).toHaveBeenCalledTimes(when === 'during-poll' ? 2 : 1)
      expect(progress).not.toHaveBeenCalled()
      expect(vi.getTimerCount()).toBe(0)
    } finally { vi.useRealTimers() }
  })

  it('uses the path-bearing endpoint only for the initial explicit binding', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({ schema: 'zhimeng.project-inspection.v1' }),
    )

    await inspectProjectRoot('C:\\Novels\\Book', {
      baseUrl: 'http://127.0.0.1:8787',
      fetcher,
    })

    const [url, init] = fetcher.mock.calls[0]
    expect(url).toBe('http://127.0.0.1:8787/api/v1/projects/inspect')
    expect(init?.method).toBe('POST')
    expect(JSON.parse(String(init?.body))).toEqual({
      root_path: 'C:\\Novels\\Book',
    })
  })

  it('reopens snapshots and files through stable binding identifiers', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(
        jsonResponse({
          schema: 'zhimeng.project-bindings.v1',
          projects: [{ project_id: 'project-abc' }],
        }),
      )
      .mockResolvedValueOnce(jsonResponse({ schema: 'zhimeng.project-inspection.v1' }))
      .mockResolvedValueOnce(jsonResponse({ schema: 'zhimeng.project-file-content.v1' }))
    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }

    const bindings = await listProjectBindings(options)
    await inspectBoundProject('project-abc', options)
    await readBoundProjectFile('project-abc', 'file-def', options)

    expect(bindings[0].project_id).toBe('project-abc')
    expect(fetcher.mock.calls.map(([url]) => url)).toEqual([
      'http://127.0.0.1:8787/api/v1/projects',
      'http://127.0.0.1:8787/api/v1/projects/project-abc/inspect',
      'http://127.0.0.1:8787/api/v1/projects/project-abc/files/file-def',
    ])
  })
})

import { describe, expect, it, vi } from 'vitest'
import {
  inspectBoundProject,
  inspectProjectRoot,
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

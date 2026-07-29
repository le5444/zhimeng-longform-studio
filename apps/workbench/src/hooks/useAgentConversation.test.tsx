import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { contextPackFixture } from '../test/contextPackFixture'
import { agentWorkflowStateFixture } from '../test/agentWorkflowFixture'
import { currentProjectKbStateFixture } from '../test/knowledgeFixture'
import {
  nlclRouteExecutionFixture,
  nlclRouteFixture,
  prewriteOperationPackageFixture,
} from '../test/nlclRouteFixture'
import type { ContextPackRecord, FormalKnowledgeSelector } from '../api/contextPacks'
import { useAgentConversation } from './useAgentConversation'

const projectId = 'project-0123456789abcdef01234567'
const sessionId = 'session-0123456789abcdef01234567'
const olderSessionId = 'session-abcdef0123456789abcdef01'
const newSessionId = 'session-fedcba9876543210fedcba98'
const runId = 'run-0123456789abcdef0123456789'
const fingerprint = 'd'.repeat(64)
const formalKnowledgeSelector: FormalKnowledgeSelector = {
  schema: 'zhimeng.formal-knowledge-selector.v1',
  source_import_id: `source-import-${'1'.repeat(32)}`,
  formalization_receipt_id: `knowledge-formalization-${'2'.repeat(32)}`,
  formalization_receipt_hash: '3'.repeat(64),
  expected_knowledge_fingerprint: '4'.repeat(64),
  cards: [{
    card_id: 'work-pressure-loop',
    version_id: `knowledge-version-${'5'.repeat(32)}`,
    content_hash: '6'.repeat(64),
  }],
}

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function sessionRecord(id: string, title: string, updatedAt: string) {
  return {
    schema: 'zhimeng.agent-session.v1',
    session_id: id,
    project_revision: {
      project_id: projectId,
      revision: 1,
      fingerprint,
      observed_at: '2026-07-19T10:00:00Z',
    },
    title,
    status: 'idle',
    revision: 1,
    executor: {},
    capabilities: {},
    correlation_id: 'correlation-0123456789abcdef',
    last_run_ordinal: 0,
    last_message_ordinal: 0,
    created_at: updatedAt,
    updated_at: updatedAt,
  }
}

function routeFor(record: ContextPackRecord) {
  const pack = record.pack
  return nlclRouteFixture({
    context_pack_id: pack.context_pack_id,
    context_pack_hash: pack.pack_hash,
    project_id: pack.project_id,
    project_revision: pack.project_revision,
    project_fingerprint: pack.project_fingerprint,
    active_chapter_id: pack.active_chapter_id,
    profile: pack.profile,
    knowledge_fingerprint: pack.knowledge_fingerprint,
    project_kb_state_id: pack.project_kb_state_id,
    project_kb_manifest_hash: pack.project_kb_manifest_hash,
  })
}

function executionFor(record: ContextPackRecord, callOrdinal = 1) {
  return nlclRouteExecutionFixture(routeFor(record), { call_ordinal: callOrdinal })
}

function operationPackageFor(record: ContextPackRecord) {
  return prewriteOperationPackageFixture(routeFor(record), {
    readiness: 'risk_draft_only',
    missing_requirements: [],
  })
}

function run(status: string) {
  return {
    schema: 'zhimeng.execution-run.v1',
    run_id: runId,
    session_id: sessionId,
    ordinal: 1,
    status,
    objective: '收紧开头',
    created_at: '2026-07-19T10:00:00Z',
    updated_at: '2026-07-19T10:00:01Z',
    started_at: status === 'queued' ? null : '2026-07-19T10:00:00Z',
    completed_at: status === 'succeeded' ? '2026-07-19T10:00:01Z' : null,
    executor: {},
    capabilities: {},
    project_revision: {
      project_id: projectId,
      revision: 1,
      fingerprint,
      observed_at: '2026-07-19T10:00:00Z',
    },
    correlation_id: 'correlation-0123456789abcdef',
    causation_id: null,
    last_activity_sequence: status === 'succeeded' ? 8 : 2,
    checkpoint_id: null,
    error: null,
  }
}

function activityEvent(sequence: number, kind: string, payload: Record<string, unknown>) {
  const eventId = `${runId}:${sequence}`
  return `id: ${eventId}\nevent: activity\ndata: ${JSON.stringify({
    schema: 'zhimeng.run-event.v1',
    event_id: eventId,
    event_type: 'activity',
    emitted_at: '2026-07-19T10:00:00Z',
    data: {
      activity: activityRecord(sequence, kind, payload),
    },
  })}\n\n`
}

function activityRecord(sequence: number, kind: string, payload: Record<string, unknown>) {
  return {
    schema: 'zhimeng.activity-item.v1',
    activity_id: `activity-${sequence.toString().padStart(8, '0')}`,
    session_id: sessionId,
    run_id: runId,
    sequence,
    created_at: '2026-07-19T10:00:00Z',
    project_revision: {
      project_id: projectId,
      revision: 1,
      fingerprint,
      observed_at: '2026-07-19T10:00:00Z',
    },
    correlation_id: 'correlation-0123456789abcdef',
    causation_id: null,
    kind,
    payload,
  }
}

function doneEvent(status = 'succeeded') {
  return `id: ${runId}:8\nevent: done\ndata: ${JSON.stringify({
    schema: 'zhimeng.run-event.v1',
    event_id: `${runId}:8`,
    event_type: 'done',
    emitted_at: '2026-07-19T10:00:01Z',
    data: { run: run(status) },
  })}\n\n`
}

function streamResponse(chunks: Array<{ delay: number; text: string }>): Response {
  const encoder = new TextEncoder()
  return new Response(
    new ReadableStream({
      start(controller) {
        let elapsed = 0
        for (const chunk of chunks) {
          elapsed += chunk.delay
          window.setTimeout(() => controller.enqueue(encoder.encode(chunk.text)), elapsed)
        }
        window.setTimeout(() => controller.close(), elapsed + 5)
      },
    }),
    { status: 200, headers: { 'Content-Type': 'text/event-stream' } },
  )
}

function Harness() {
  const conversation = useAgentConversation({
    enabled: true,
    projectId,
    projectRevision: 1,
    projectFingerprint: fingerprint,
    activeChapterId: 'chapter-001',
  })
  return <div>
    <span data-testid="availability">{conversation.availability}</span>
    <span data-testid="stream">{conversation.streamingContent}</span>
    <span data-testid="status">{conversation.activeRun?.status ?? 'none'}</span>
    <span data-testid="workflow-state">{conversation.workflow?.resume_state ?? 'none'}</span>
    <span data-testid="running">{String(conversation.running)}</span>
    <span data-testid="busy">{String(conversation.busy)}</span>
    <span data-testid="resuming">{String(conversation.resuming)}</span>
    <span data-testid="session-id">{conversation.session?.session_id ?? 'none'}</span>
    <span data-testid="session-title">{conversation.session?.title ?? 'none'}</span>
    <span data-testid="session-count">{conversation.sessions.length}</span>
    <span data-testid="context-state">{conversation.contextState}</span>
    <span data-testid="pending-context">{conversation.pendingContextPack?.pack.objective ?? 'none'}</span>
    <span data-testid="conversation-error">{conversation.error ?? 'none'}</span>
    <span data-testid="activity-count">{conversation.activities.length}</span>
    <span data-testid="activity-sequences">{conversation.activities.map((activity) => activity.sequence).join(',')}</span>
    <div>{conversation.messages.map((message) => <p key={message.message_id}>{message.content}</p>)}</div>
    <button type="button" onClick={() => void conversation.compileContext('收紧开头').catch(() => undefined)}>编译</button>
    <button type="button" onClick={() => void conversation.compileContext('收紧开头', 'chapter', formalKnowledgeSelector, 'chapter-002').catch(() => undefined)}>编译指定章节</button>
    <button type="button" onClick={() => void conversation.confirmPendingContext().catch(() => undefined)}>确认</button>
    <button type="button" onClick={() => void conversation.resume().catch(() => undefined)}>继续工作流</button>
    <button type="button" onClick={() => void conversation.selectConversation(olderSessionId).catch(() => undefined)}>切换旧对话</button>
    <button type="button" onClick={() => void conversation.createConversation().catch(() => undefined)}>新建对话</button>
  </div>
}

function SwitchHarness({ project }: { project: string }) {
  const conversation = useAgentConversation({
    enabled: true,
    projectId: project,
    projectRevision: 1,
    projectFingerprint: fingerprint,
    activeChapterId: null,
  })
  return <span data-testid="switch-availability">{conversation.availability}</span>
}

function ContextHarness({
  revision = 1,
  chapter = 'chapter-001',
}: {
  revision?: number
  chapter?: string | null
}) {
  const conversation = useAgentConversation({
    enabled: true,
    projectId,
    projectRevision: revision,
    projectFingerprint: fingerprint,
    activeChapterId: chapter,
  })
  return <div>
    <span data-testid="context-harness-state">{conversation.contextState}</span>
    <span data-testid="context-harness-objective">{conversation.pendingContextPack?.pack.objective ?? 'none'}</span>
    <ul>{conversation.contextStaleReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>
    <button type="button" onClick={() => void conversation.refreshPendingContext().catch(() => undefined)}>刷新 Pack</button>
  </div>
}

describe('useAgentConversation', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('does not restore a session bound to an older provider setting', async () => {
    const fetcher = vi.fn<typeof fetch>((input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: { instance_id: `provider-config-${'a'.repeat(64)}` },
          capabilities: {},
          detail: 'Writing Agent provider is configured',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-session-list.v1',
          sessions: [{
            schema: 'zhimeng.agent-session.v1',
            session_id: sessionId,
            project_revision: {
              project_id: projectId,
              revision: 1,
              fingerprint,
              observed_at: '2026-07-19T10:00:00Z',
            },
            title: '旧模型对话',
            status: 'idle',
            revision: 1,
            executor: { instance_id: `provider-config-${'b'.repeat(64)}` },
            capabilities: {},
            correlation_id: 'correlation-0123456789abcdef',
            last_run_ordinal: 1,
            last_message_ordinal: 2,
            created_at: '2026-07-19T10:00:00Z',
            updated_at: '2026-07-19T10:00:00Z',
          }],
        }))
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    expect(await screen.findByTestId('availability')).toHaveTextContent('available')
    await waitFor(() => {
      expect(screen.getByTestId('context-state')).toHaveTextContent('idle')
    })
    expect(screen.getByTestId('session-id')).toHaveTextContent('none')
    expect(fetcher).not.toHaveBeenCalledWith(
      expect.stringContaining(`/api/v1/agent/sessions/${sessionId}/messages`),
      expect.anything(),
    )
  })

  it('switches persisted conversations and creates a clean conversation through the existing session API', async () => {
    const user = userEvent.setup()
    const current = sessionRecord(sessionId, '当前改稿', '2026-07-29T09:00:00Z')
    const older = sessionRecord(olderSessionId, '旧伏笔检查', '2026-07-28T09:00:00Z')
    const created = sessionRecord(newSessionId, '新对话', '2026-07-29T10:00:00Z')
    let createCalls = 0
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'Writing Agent provider is configured',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        if (init?.method === 'POST') {
          createCalls += 1
          return Promise.resolve(jsonResponse(created, 201))
        }
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-session-list.v1',
          sessions: [current, older],
        }))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${olderSessionId}`)) {
        return Promise.resolve(jsonResponse(older))
      }
      for (const id of [sessionId, olderSessionId, newSessionId]) {
        if (url.includes(`/api/v1/agent/sessions/${id}/messages`)) {
          return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
        }
        if (url.includes(`/api/v1/agent/sessions/${id}/runs?`)) {
          return Promise.resolve(jsonResponse({ runs: [] }))
        }
        if (url.includes(`/api/v1/agent/sessions/${id}/context-packs?`)) {
          return Promise.resolve(jsonResponse({
            schema: 'zhimeng.context-pack-list.v1',
            session_id: id,
            context_packs: [],
          }))
        }
      }
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    expect(await screen.findByTestId('session-title')).toHaveTextContent('当前改稿')
    expect(screen.getByTestId('session-count')).toHaveTextContent('2')

    await user.click(screen.getByRole('button', { name: '切换旧对话' }))
    await waitFor(() => expect(screen.getByTestId('session-title')).toHaveTextContent('旧伏笔检查'))

    await user.click(screen.getByRole('button', { name: '新建对话' }))
    await waitFor(() => expect(screen.getByTestId('session-id')).toHaveTextContent(newSessionId))
    expect(screen.getByTestId('session-count')).toHaveTextContent('3')
    expect(createCalls).toBe(1)
  })

  it('shows the first delta early, reconnects with Last-Event-ID, and restores canonical history', async () => {
    let sessionCreated = false
    let completed = false
    let streamCalls = 0
    let runCreateCalls = 0
    let projectKbReads = 0
    const replayHeaders: Array<string | null> = []
    const pack = contextPackFixture({
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
    })
    let runRequest: Record<string, unknown> | null = null
    let contextRequest: Record<string, unknown> | null = null
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith(`/api/v1/projects/${projectId}/knowledge/kb-state`)) {
        projectKbReads += 1
        return Promise.resolve(jsonResponse(currentProjectKbStateFixture({
          projectId,
          revision: 1,
          fingerprint,
        })))
      }
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'Writing Agent provider is configured',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        if (init?.method === 'POST') {
          sessionCreated = true
          return Promise.resolve(jsonResponse({
            schema: 'zhimeng.agent-session.v1',
            session_id: sessionId,
            status: 'idle',
          }, 201))
        }
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-session-list.v1',
          sessions: sessionCreated ? [{
            schema: 'zhimeng.agent-session.v1',
            session_id: sessionId,
            status: 'idle',
          }] : [],
        }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({
          messages: completed ? [
            {
              schema: 'zhimeng.agent-message.v1',
              message_id: 'message-user-00000001',
              session_id: sessionId,
              run_id: runId,
              ordinal: 1,
              role: 'user',
              format: 'plain',
              content: '收紧开头',
              created_at: '2026-07-19T10:00:00Z',
            },
            {
              schema: 'zhimeng.agent-message.v1',
              message_id: 'message-agent-00000001',
              session_id: sessionId,
              run_id: runId,
              ordinal: 2,
              role: 'assistant',
              format: 'markdown',
              content: '第一段第二段',
              created_at: '2026-07-19T10:00:01Z',
            },
          ] : [],
          last_ordinal: completed ? 2 : 0,
        }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: completed ? [run('succeeded')] : [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: completed ? [{ ...pack, consumed_by_run_id: runId }] : [],
        }))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-activity-list.v1',
          run_id: runId,
          activities: completed ? [
            activityRecord(5, 'message', { role: 'assistant', phase: 'delta', delta: '第一段', content: null }),
            activityRecord(6, 'message', { role: 'assistant', phase: 'delta', delta: '第二段', content: null }),
            activityRecord(8, 'run_state', { status: 'succeeded', previous_status: 'running', detail: 'done' }),
          ] : [],
          last_sequence: completed ? 8 : 0,
        }))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/context-packs`)) {
        contextRequest = JSON.parse(String(init?.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(pack, 201))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/runs`)) {
        runCreateCalls += 1
        runRequest = JSON.parse(String(init?.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse({
          run: run('queued'),
          replayed: false,
          user_message: {
            schema: 'zhimeng.agent-message.v1',
            message_id: 'message-user-00000001',
            session_id: sessionId,
            run_id: runId,
            ordinal: 1,
            role: 'user',
            format: 'plain',
            content: '收紧开头',
            created_at: '2026-07-19T10:00:00Z',
          },
        }, 202))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
        streamCalls += 1
        replayHeaders.push(new Headers(init?.headers).get('Last-Event-ID'))
        if (streamCalls === 1) {
          return Promise.resolve(streamResponse([{
            delay: 10,
            text: activityEvent(5, 'message', {
              role: 'assistant',
              phase: 'delta',
              delta: '第一段',
              content: null,
            }),
          }]))
        }
        completed = true
        return Promise.resolve(streamResponse([
          {
            delay: 20,
            text: activityEvent(6, 'message', {
              role: 'assistant',
              phase: 'delta',
              delta: '第二段',
              content: null,
            }),
          },
          {
            delay: 10,
            text: activityEvent(8, 'run_state', {
              status: 'succeeded',
              previous_status: 'running',
              detail: 'done',
            }) + doneEvent(),
          },
        ]))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const user = userEvent.setup()
    const first = render(<Harness />)
    expect(await screen.findByTestId('availability')).toHaveTextContent('available')
    await user.click(screen.getByRole('button', { name: '编译指定章节' }))
    expect(await screen.findByTestId('pending-context')).toHaveTextContent('收紧开头')
    expect(screen.getByTestId('context-state')).toHaveTextContent('ready')
    expect(runCreateCalls).toBe(0)
    expect(contextRequest).toMatchObject({
      objective: '收紧开头',
      profile: 'chapter',
      active_chapter_id: 'chapter-002',
      formal_knowledge_selector: formalKnowledgeSelector,
      project_kb_state_id: pack.pack.project_kb_state_id,
      project_kb_manifest_hash: pack.pack.project_kb_manifest_hash,
    })

    await user.click(screen.getByRole('button', { name: '确认' }))
    expect(runRequest).toMatchObject({
      message: '收紧开头',
      writer_mode: 'repair_only',
      context_pack_id: pack.pack.context_pack_id,
      expected_context_pack_hash: pack.pack.pack_hash,
    })
    expect(projectKbReads).toBe(2)
    await waitFor(() => expect(screen.getByTestId('stream')).toHaveTextContent('第一段'))
    expect(screen.getByTestId('stream')).not.toHaveTextContent('第二段')
    expect(await screen.findByText('第一段第二段')).toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'))
    expect(streamCalls).toBe(2)
    expect(replayHeaders).toEqual([null, `${runId}:5`])

    first.unmount()
    render(<Harness />)
    expect(await screen.findByText('第一段第二段')).toBeInTheDocument()
    expect(screen.getByTestId('status')).toHaveTextContent('succeeded')
    expect(screen.getByTestId('activity-count')).toHaveTextContent('3')
    expect(screen.getByTestId('activity-sequences')).toHaveTextContent('5,6,8')
    expect(streamCalls).toBe(2)
  })

  it.each([
    ['missing', '尚未生成'],
    ['stale', '已过期'],
    ['tampered', '校验失败'],
  ] as const)('blocks Context compilation before session creation when the project KB is %s', async (status, message) => {
    let sessionCreateCalls = 0
    let contextCreateCalls = 0
    const unavailable = {
      ...currentProjectKbStateFixture({ projectId, revision: 1, fingerprint }),
      status,
      ...(status === 'missing' ? { pointer: null, manifest: null } : {}),
    }
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        if (init?.method === 'POST') sessionCreateCalls += 1
        return Promise.resolve(jsonResponse({ sessions: [] }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/knowledge/kb-state`)) {
        return Promise.resolve(jsonResponse(unavailable))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/context-packs`)) {
        contextCreateCalls += 1
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const user = userEvent.setup()
    render(<Harness />)
    expect(await screen.findByTestId('availability')).toHaveTextContent('available')
    await user.click(screen.getByRole('button', { name: '编译' }))

    await waitFor(() => expect(screen.getByTestId('conversation-error')).toHaveTextContent(message))
    expect(screen.getByTestId('pending-context')).toHaveTextContent('none')
    expect(sessionCreateCalls).toBe(0)
    expect(contextCreateCalls).toBe(0)
  })

  it('restores the newest unconsumed Pack, detects history and project drift, and recompiles it', async () => {
    const historicalMessages = [
      {
        schema: 'zhimeng.agent-message.v1',
        message_id: 'message-user-history',
        session_id: sessionId,
        run_id: 'run-history',
        ordinal: 1,
        role: 'user',
        format: 'plain',
        content: '上一轮任务',
        created_at: '2026-07-19T09:00:00Z',
      },
      {
        schema: 'zhimeng.agent-message.v1',
        message_id: 'message-assistant-history',
        session_id: sessionId,
        run_id: 'run-history',
        ordinal: 2,
        role: 'assistant',
        format: 'markdown',
        content: '上一轮结果',
        created_at: '2026-07-19T09:00:01Z',
      },
    ]
    const stalePack = contextPackFixture({
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
      history_through_ordinal: 0,
      history_message_count: 0,
    })
    const refreshedPack = contextPackFixture({
      context_pack_id: 'context-pack-refreshed',
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
      history_through_ordinal: 2,
      history_message_count: 2,
      exact_provider_messages: [
        { role: 'system', content: '项目控制卡' },
        { role: 'user', content: '上一轮任务' },
        { role: 'assistant', content: '上一轮结果' },
        { role: 'user', content: '收紧开头' },
      ],
      pack_hash: '6'.repeat(64),
    })
    let refreshRequest: Record<string, unknown> | null = null
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith(`/api/v1/projects/${projectId}/knowledge/kb-state`)) {
        return Promise.resolve(jsonResponse(currentProjectKbStateFixture({
          projectId,
          revision: 1,
          fingerprint,
        })))
      }
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'idle',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: historicalMessages, last_ordinal: 2 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [stalePack],
        }))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${stalePack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(stalePack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${stalePack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(stalePack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${stalePack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(stalePack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${stalePack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(stalePack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${stalePack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(stalePack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${refreshedPack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(refreshedPack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${refreshedPack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(refreshedPack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${refreshedPack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(refreshedPack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${refreshedPack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(refreshedPack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${refreshedPack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(refreshedPack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/context-packs`)) {
        refreshRequest = JSON.parse(String(init?.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse(refreshedPack, 201))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const user = userEvent.setup()
    const view = render(<ContextHarness />)
    expect(await screen.findByTestId('context-harness-objective')).toHaveTextContent('收紧开头')
    expect(screen.getByTestId('context-harness-state')).toHaveTextContent('stale')
    expect(screen.getByText(/会话历史已从 ordinal 0 变为 2/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '刷新 Pack' }))
    await waitFor(() => expect(screen.getByTestId('context-harness-state')).toHaveTextContent('ready'))
    expect(refreshRequest).toMatchObject({
      objective: '收紧开头',
      profile: 'chapter',
      expected_project_revision: 1,
      active_chapter_id: 'chapter-001',
      project_kb_state_id: refreshedPack.pack.project_kb_state_id,
      project_kb_manifest_hash: refreshedPack.pack.project_kb_manifest_hash,
    })

    view.rerender(<ContextHarness revision={2} chapter="chapter-002" />)
    expect(screen.getByTestId('context-harness-state')).toHaveTextContent('stale')
    expect(screen.getByText(/项目 revision 已从 1 变为 2/)).toBeInTheDocument()
    expect(screen.getByText('当前章节已经变化')).toBeInTheDocument()
  })

  it('rechecks session history immediately before consuming a pending Pack', async () => {
    const pack = contextPackFixture({
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
      history_through_ordinal: 0,
    })
    let historyChanged = false
    let runCreateCalls = 0
    const fetcher = vi.fn<typeof fetch>((input) => {
      const url = String(input)
      if (url.endsWith(`/api/v1/projects/${projectId}/knowledge/kb-state`)) {
        return Promise.resolve(jsonResponse(currentProjectKbStateFixture({
          projectId,
          revision: 1,
          fingerprint,
        })))
      }
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'idle',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({
          messages: [],
          last_ordinal: historyChanged ? 1 : 0,
        }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [pack],
        }))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/runs`)) {
        runCreateCalls += 1
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const user = userEvent.setup()
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('context-state')).toHaveTextContent('ready'))
    historyChanged = true
    await user.click(screen.getByRole('button', { name: '确认' }))

    await waitFor(() => expect(screen.getByTestId('context-state')).toHaveTextContent('stale'))
    expect(screen.getByTestId('conversation-error')).toHaveTextContent('会话历史已变化，请重新编译 Context Pack')
    expect(runCreateCalls).toBe(0)
  })

  it('rechecks the project KB before confirmation and refuses a changed pair', async () => {
    const pack = contextPackFixture({
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
      history_through_ordinal: 0,
    })
    let runCreateCalls = 0
    const fetcher = vi.fn<typeof fetch>((input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'idle',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [pack],
        }))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/knowledge/kb-state`)) {
        return Promise.resolve(jsonResponse(currentProjectKbStateFixture({
          projectId,
          revision: 1,
          fingerprint,
          stateId: `project-kb-state-${'b'.repeat(32)}`,
          manifestHash: 'c'.repeat(64),
        })))
      }
      if (url.endsWith(`/api/v1/agent/sessions/${sessionId}/runs`)) runCreateCalls += 1
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const user = userEvent.setup()
    render(<Harness />)
    await waitFor(() => expect(screen.getByTestId('context-state')).toHaveTextContent('ready'))
    await user.click(screen.getByRole('button', { name: '确认' }))

    await waitFor(() => expect(screen.getByTestId('context-state')).toHaveTextContent('stale'))
    expect(screen.getByTestId('conversation-error')).toHaveTextContent('项目知识快照已变化，请重新编译 Context Pack')
    expect(runCreateCalls).toBe(0)
  })

  it('uses the authoritative history ordinal when the displayed message page is truncated', async () => {
    const pack = contextPackFixture({
      project_id: projectId,
      session_id: sessionId,
      project_fingerprint: fingerprint,
      history_through_ordinal: 501,
      history_message_count: 501,
    })
    const fetcher = vi.fn<typeof fetch>((input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'idle',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({
          messages: [{
            schema: 'zhimeng.agent-message.v1',
            message_id: 'message-page-tail',
            session_id: sessionId,
            run_id: 'run-page-tail',
            ordinal: 500,
            role: 'assistant',
            format: 'markdown',
            content: '展示页最后一条',
            created_at: '2026-07-20T00:00:00Z',
          }],
          last_ordinal: 501,
        }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [pack],
        }))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route`)) {
        return Promise.resolve(jsonResponse(routeFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/prewrite-operation-package`)) {
        return Promise.resolve(jsonResponse(operationPackageFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/1`)) {
        return Promise.resolve(jsonResponse(executionFor(pack)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/2`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      if (url.endsWith(`/api/v1/agent/context-packs/${pack.pack.context_pack_id}/nlcl-route/executions/3`)) {
        return Promise.resolve(jsonResponse(executionFor(pack, 2)))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    render(<ContextHarness />)

    expect(await screen.findByTestId('context-harness-state')).toHaveTextContent('ready')
    expect(screen.queryByText(/会话历史已从 ordinal/)).not.toBeInTheDocument()
  })

  it('restores a paused workflow and resumes the same Run from its SSE cursor', async () => {
    const user = userEvent.setup()
    const workflow = agentWorkflowStateFixture({ runId, projectId })
    let resumeBody: Record<string, unknown> | null = null
    let lastEventId: string | null = null
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'active',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [run('paused')] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [],
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-activity-list.v1',
          run_id: runId,
          activities: [activityRecord(2, 'run_state', {
            status: 'paused',
            previous_status: 'running',
            detail: 'restart recovery',
          })],
          last_sequence: 2,
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/workflow`)) {
        return Promise.resolve(jsonResponse(workflow))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/resume`)) {
        resumeBody = JSON.parse(String(init?.body)) as Record<string, unknown>
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-run-resume.v1',
          run: run('running'),
          accepted: true,
          workflow_plan_id: workflow.plan.workflow_plan_id,
          resumed_from_checkpoint_id: workflow.last_checkpoint_id,
          resumed_from_checkpoint_hash: workflow.last_checkpoint_hash,
          next_action: 'workflow_step',
        }, 202))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
        lastEventId = new Headers(init?.headers).get('Last-Event-ID')
        return Promise.resolve(streamResponse([{ delay: 100, text: doneEvent() }]))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('workflow-state')).toHaveTextContent('resumable'))
    expect(screen.getByTestId('status')).toHaveTextContent('paused')
    expect(screen.getByTestId('running')).toHaveTextContent('false')
    expect(screen.getByTestId('busy')).toHaveTextContent('true')
    expect(screen.getByTestId('activity-count')).toHaveTextContent('1')

    await user.click(screen.getByRole('button', { name: '继续工作流' }))

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('running'))
    expect(lastEventId).toBe(`${runId}:2`)
    expect(resumeBody).toEqual({
      schema: 'zhimeng.agent-run-resume-request.v1',
      expected_workflow_plan_id: workflow.plan.workflow_plan_id,
      expected_plan_hash: workflow.plan.plan_hash,
      expected_checkpoint_id: workflow.last_checkpoint_id,
      expected_checkpoint_hash: workflow.last_checkpoint_hash,
    })
    expect(screen.getByTestId('activity-count')).toHaveTextContent('1')
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'))
  })

  it.each(['response_lost', 'conflict'] as const)(
    'reconciles a Resume %s without posting the command twice',
    async (failureMode) => {
      const user = userEvent.setup()
      const workflow = agentWorkflowStateFixture({ runId, projectId })
      const runningWorkflow = {
        ...workflow,
        run_status: 'running' as const,
        resume_state: 'running' as const,
        resume_allowed: false,
        reason_code: 'workflow_execution_active',
      }
      let workflowCalls = 0
      let resumeCalls = 0
      let lastEventId: string | null = null
      const fetcher = vi.fn<typeof fetch>((input, init) => {
        const url = String(input)
        if (url.endsWith('/api/v1/agent/status')) {
          return Promise.resolve(jsonResponse({
            schema: 'zhimeng.agent-status.v1',
            available: true,
            executor: {},
            capabilities: {},
            detail: 'ready',
          }))
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
          return Promise.resolve(jsonResponse({ sessions: [{
            schema: 'zhimeng.agent-session.v1',
            session_id: sessionId,
            status: 'active',
          }] }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
          return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
          return Promise.resolve(jsonResponse({ runs: [run('paused')] }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
          return Promise.resolve(jsonResponse({ context_packs: [] }))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
          return Promise.resolve(jsonResponse({
            run_id: runId,
            activities: [activityRecord(2, 'run_state', {
              status: 'paused',
              previous_status: 'running',
              detail: 'restart recovery',
            })],
            last_sequence: 2,
          }))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/workflow`)) {
          workflowCalls += 1
          return Promise.resolve(jsonResponse(workflowCalls === 1 ? workflow : runningWorkflow))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/resume`)) {
          resumeCalls += 1
          if (failureMode === 'response_lost') {
            return Promise.reject(new TypeError('connection closed after claim'))
          }
          return Promise.resolve(jsonResponse({
            error: {
              code: 'agent_run_already_running',
              message: 'Workflow Run is already executing',
              retryable: false,
            },
          }, 409))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
          lastEventId = new Headers(init?.headers).get('Last-Event-ID')
          return Promise.resolve(streamResponse([{ delay: 10, text: doneEvent() }]))
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`))
      })
      vi.stubGlobal('fetch', fetcher)

      render(<Harness />)
      await waitFor(() => expect(screen.getByTestId('workflow-state')).toHaveTextContent('resumable'))

      await user.click(screen.getByRole('button', { name: '继续工作流' }))

      await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'))
      expect(screen.getByTestId('conversation-error')).toHaveTextContent('none')
      expect(resumeCalls).toBe(1)
      expect(workflowCalls).toBe(2)
      expect(lastEventId).toBe(`${runId}:2`)
    },
  )

  it('hydrates durable state after a live SSE transition pauses the Run', async () => {
    const user = userEvent.setup()
    const workflow = agentWorkflowStateFixture({ runId, projectId })
    let workflowCalls = 0
    const eventCursors: Array<string | null> = []
    let pauseDelivered = false
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'active',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [run('running')] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({ context_packs: [] }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          run_id: runId,
          activities: [activityRecord(2, 'run_state', {
            status: 'running',
            previous_status: 'starting',
            detail: 'connected',
          })],
          last_sequence: 2,
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
        eventCursors.push(new Headers(init?.headers).get('Last-Event-ID'))
        if (eventCursors.length === 1) {
          pauseDelivered = true
          return Promise.resolve(streamResponse([{
            delay: 10,
            text: activityEvent(3, 'run_state', {
              status: 'paused',
              previous_status: 'running',
              detail: 'restart recovery',
            }),
          }]))
        }
        return Promise.resolve(streamResponse([{ delay: 10, text: doneEvent() }]))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/workflow`)) {
        expect(pauseDelivered).toBe(true)
        workflowCalls += 1
        return Promise.resolve(jsonResponse(workflow))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/resume`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-run-resume.v1',
          run: run('running'),
          accepted: true,
          workflow_plan_id: workflow.plan.workflow_plan_id,
          resumed_from_checkpoint_id: workflow.last_checkpoint_id,
          resumed_from_checkpoint_hash: workflow.last_checkpoint_hash,
          next_action: 'workflow_step',
        }, 202))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('workflow-state')).toHaveTextContent('resumable'))
    expect(screen.getByTestId('status')).toHaveTextContent('paused')
    expect(screen.getByTestId('running')).toHaveTextContent('false')
    expect(screen.getByTestId('busy')).toHaveTextContent('true')
    expect(screen.getByTestId('activity-sequences')).toHaveTextContent('2,3')
    expect(screen.getByTestId('conversation-error')).toHaveTextContent('none')
    expect(workflowCalls).toBe(1)

    await user.click(screen.getByRole('button', { name: '继续工作流' }))

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'))
    expect(eventCursors).toEqual([`${runId}:2`, `${runId}:3`])
    expect(screen.getByTestId('activity-sequences')).toHaveTextContent('2,3')
  })

  it.each(['failed', 'cancelled'] as const)(
    'hydrates the Provider boundary after a live %s terminal event',
    async (terminalStatus) => {
      const workflow = agentWorkflowStateFixture({
        runId,
        projectId,
        runStatus: terminalStatus,
        providerBoundary: true,
      })
      let doneDelivered = false
      let workflowCalls = 0
      const fetcher = vi.fn<typeof fetch>((input) => {
        const url = String(input)
        if (url.endsWith('/api/v1/agent/status')) {
          return Promise.resolve(jsonResponse({
            schema: 'zhimeng.agent-status.v1',
            available: true,
            executor: {},
            capabilities: {},
            detail: 'ready',
          }))
        }
        if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
          return Promise.resolve(jsonResponse({ sessions: [{
            schema: 'zhimeng.agent-session.v1',
            session_id: sessionId,
            status: 'active',
          }] }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
          return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
          return Promise.resolve(jsonResponse({ runs: [run('running')] }))
        }
        if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
          return Promise.resolve(jsonResponse({ context_packs: [] }))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
          return Promise.resolve(jsonResponse({
            run_id: runId,
            activities: [activityRecord(2, 'run_state', {
              status: 'running',
              previous_status: 'starting',
              detail: 'connected',
            })],
            last_sequence: 2,
          }))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
          doneDelivered = true
          return Promise.resolve(streamResponse([{
            delay: 10,
            text: doneEvent(terminalStatus),
          }]))
        }
        if (url.endsWith(`/api/v1/agent/runs/${runId}/workflow`)) {
          expect(doneDelivered).toBe(true)
          workflowCalls += 1
          return Promise.resolve(jsonResponse(workflow))
        }
        return Promise.reject(new Error(`Unexpected URL: ${url}`))
      })
      vi.stubGlobal('fetch', fetcher)

      render(<Harness />)

      await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent(terminalStatus))
      await waitFor(() => expect(screen.getByTestId('workflow-state')).toHaveTextContent('provider_dispatch_uncertain'))
      expect(screen.getByTestId('conversation-error')).toHaveTextContent('none')
      expect(workflowCalls).toBe(1)
      expect(fetcher.mock.calls.some(([input]) => String(input).endsWith(`/api/v1/agent/runs/${runId}/resume`))).toBe(false)
    },
  )

  it.each(['interrupted', 'failed', 'cancelled'] as const)(
    'keeps the legacy %s UI when no durable workflow exists',
    async (terminalStatus) => {
    const fetcher = vi.fn<typeof fetch>((input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'idle',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [run(terminalStatus)] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({ context_packs: [] }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          run_id: runId,
          activities: [],
          last_sequence: 0,
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/workflow`)) {
        return Promise.resolve(jsonResponse({
          error: { code: 'agent_resource_not_found', message: 'Workflow not found' },
        }, 404))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent(terminalStatus))
    expect(screen.getByTestId('workflow-state')).toHaveTextContent('none')
    expect(screen.getByTestId('conversation-error')).toHaveTextContent('none')
    },
  )

  it('restores Activity history, resumes SSE from last_sequence, and ignores replayed IDs', async () => {
    let lastEventId: string | null = null
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'active',
        }] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [run('running')] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [],
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-activity-list.v1',
          run_id: runId,
          activities: [
            activityRecord(1, 'message', {
              role: 'assistant',
              phase: 'delta',
              delta: '刷新前已生成',
              content: null,
            }),
            activityRecord(2, 'run_state', {
              status: 'running',
              previous_status: 'starting',
              detail: 'connected',
            }),
          ],
          last_sequence: 2,
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
        lastEventId = new Headers(init?.headers).get('Last-Event-ID')
        return Promise.resolve(streamResponse([{
          delay: 100,
          text: activityEvent(2, 'run_state', {
            status: 'running',
            previous_status: 'starting',
            detail: 'duplicate replay',
          }) + activityEvent(3, 'run_state', {
            status: 'running',
            previous_status: 'running',
            detail: 'new event',
          }) + doneEvent(),
        }]))
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    render(<Harness />)

    await waitFor(() => expect(screen.getByTestId('stream')).toHaveTextContent('刷新前已生成'))
    await waitFor(() => expect(screen.getByTestId('status')).toHaveTextContent('succeeded'))
    expect(lastEventId).toBe(`${runId}:2`)
    expect(screen.getByTestId('activity-count')).toHaveTextContent('3')
    expect(screen.getByTestId('activity-sequences')).toHaveTextContent('1,2,3')
  })

  it('aborts the active event stream when the project changes', async () => {
    const nextProjectId = 'project-fedcba9876543210fedcba98'
    let streamSignal: AbortSignal | null = null
    const fetcher = vi.fn<typeof fetch>((input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/agent/status')) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-status.v1',
          available: true,
          executor: {},
          capabilities: {},
          detail: 'ready',
        }))
      }
      if (url.endsWith(`/api/v1/projects/${projectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [{
          schema: 'zhimeng.agent-session.v1',
          session_id: sessionId,
          status: 'active',
        }] }))
      }
      if (url.endsWith(`/api/v1/projects/${nextProjectId}/agent/sessions`)) {
        return Promise.resolve(jsonResponse({ sessions: [] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/messages`)) {
        return Promise.resolve(jsonResponse({ messages: [], last_ordinal: 0 }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/runs?`)) {
        return Promise.resolve(jsonResponse({ runs: [run('running')] }))
      }
      if (url.includes(`/api/v1/agent/sessions/${sessionId}/context-packs?`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.context-pack-list.v1',
          session_id: sessionId,
          context_packs: [],
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/activities?after_sequence=0`)) {
        return Promise.resolve(jsonResponse({
          schema: 'zhimeng.agent-activity-list.v1',
          run_id: runId,
          activities: [activityRecord(2, 'run_state', {
            status: 'running',
            previous_status: 'starting',
            detail: 'connected',
          })],
          last_sequence: 2,
        }))
      }
      if (url.endsWith(`/api/v1/agent/runs/${runId}/events`)) {
        streamSignal = init?.signal ?? null
        return new Promise<Response>((_resolve, reject) => {
          streamSignal?.addEventListener('abort', () => {
            reject(new DOMException('aborted', 'AbortError'))
          }, { once: true })
        })
      }
      return Promise.reject(new Error(`Unexpected URL: ${url}`))
    })
    vi.stubGlobal('fetch', fetcher)

    const view = render(<SwitchHarness project={projectId} />)
    await waitFor(() => expect(streamSignal).not.toBeNull())
    view.rerender(<SwitchHarness project={nextProjectId} />)

    await waitFor(() => expect(streamSignal?.aborted).toBe(true))
  })
})

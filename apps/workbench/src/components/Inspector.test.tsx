import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentRun, AgentRunStatus, AgentSession } from '../api/agent'
import type { FormalKnowledgeSelector } from '../api/contextPacks'
import type { RuntimeSnapshot } from '../api/runtime'
import type { AgentConversationState } from '../hooks/useAgentConversation'
import { agentWorkflowStateFixture } from '../test/agentWorkflowFixture'
import { agentCandidateOutputFixture } from '../test/agentCandidateOutputFixture'
import { providerRecoveryFixture } from '../test/providerRecoveryFixture'
import { contextPackFixture } from '../test/contextPackFixture'
import {
  nlclRouteExecutionFixture,
  nlclRouteExecutionReceiptFixture,
  nlclRouteFixture,
  prewriteOperationPackageFixture,
} from '../test/nlclRouteFixture'
import { Inspector } from './Inspector'

vi.mock('./ChangeSetReview', () => ({
  ChangeSetReview: ({ preferredChangeSetId }: { preferredChangeSetId?: string | null }) => (
    <div data-testid="changeset-target">{preferredChangeSetId ?? 'none'}</div>
  ),
}))

vi.mock('./TomatoImportPanel', () => ({
  TomatoImportPanel: ({ onReviewDraft }: { onReviewDraft?: (sourceImportId: string) => void }) => (
    <button type="button" onClick={() => onReviewDraft?.('source-import-project-a')}>
      打开测试资料批次
    </button>
  ),
}))

vi.mock('./KnowledgeReviewPanel', () => ({
  KnowledgeReviewPanel: ({
    projectId,
    sourceImportId,
    onStartWriting,
  }: {
    projectId?: string | null
    sourceImportId?: string | null
    onStartWriting?: (selector: FormalKnowledgeSelector) => Promise<void>
  }) => (
    <div>
      <div data-testid="knowledge-scope">
        {projectId ?? 'application'}|{sourceImportId ?? 'all'}
      </div>
      {onStartWriting && <button type="button" onClick={() => void onStartWriting(formalKnowledgeSelector)}>使用这批知识开始写作</button>}
    </div>
  ),
}))

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

const runtime: RuntimeSnapshot = {
  connection: 'online',
  ready: true,
  serviceStatus: 'ready',
  runStatus: null,
  version: '0.1.0',
  detail: 'Runtime 已就绪',
  checkedAt: '2026-07-19T09:00:00Z',
}

const currentSession: AgentSession = {
  schema: 'zhimeng.agent-session.v1',
  session_id: 'session-0123456789abcdef01234567',
  project_revision: {
    project_id: 'project-real',
    revision: 1,
    fingerprint: 'a'.repeat(64),
    observed_at: '2026-07-29T08:00:00Z',
  },
  title: '修改第二章开头',
  status: 'idle',
  revision: 1,
  executor: {},
  capabilities: {},
  correlation_id: 'correlation-0123456789abcdef',
  last_run_ordinal: 1,
  last_message_ordinal: 2,
  created_at: '2026-07-29T08:00:00Z',
  updated_at: '2026-07-29T08:05:00Z',
}

const olderSession: AgentSession = {
  ...currentSession,
  session_id: 'session-abcdef0123456789abcdef01',
  title: '查找人物伏笔',
  updated_at: '2026-07-28T08:05:00Z',
}

const conversation: AgentConversationState = {
  availability: 'unavailable',
  detail: 'Writing Agent provider is not configured',
  sessions: [],
  session: null,
  messages: [],
  streamingContent: '',
  activeRun: null,
  workflow: null,
  activities: [],
  pendingContextPack: null,
  pendingNlclRoute: null,
  pendingNlclRouteExecutions: [],
  pendingPrewriteOperationPackage: null,
  activeContextPack: null,
  activeNlclRoute: null,
  activeNlclRouteExecutions: [],
  activePrewriteOperationPackage: null,
  contextState: 'idle',
  contextStaleReasons: [],
  running: false,
  resuming: false,
  switchingSession: false,
  busy: false,
  error: null,
  compileContext: vi.fn().mockResolvedValue(undefined),
  confirmPendingContext: vi.fn().mockResolvedValue(undefined),
  discardPendingContext: vi.fn(),
  refreshPendingContext: vi.fn().mockResolvedValue(undefined),
  openContextPackForReview: vi.fn().mockResolvedValue(undefined),
  cancel: vi.fn().mockResolvedValue(undefined),
  resume: vi.fn().mockResolvedValue(undefined),
  createConversation: vi.fn().mockResolvedValue(undefined),
  selectConversation: vi.fn().mockResolvedValue(undefined),
  refresh: vi.fn(),
}

function recoveryRun(status: AgentRunStatus, retryable: boolean): AgentRun {
  return {
    schema: 'zhimeng.execution-run.v1',
    run_id: 'run-0123456789abcdef0123456789',
    session_id: 'session-0123456789abcdef01234567',
    ordinal: 2,
    status,
    objective: '收紧第一章开场，但保留黄铜钥匙',
    created_at: '2026-07-20T05:00:00Z',
    updated_at: '2026-07-20T05:00:03Z',
    started_at: '2026-07-20T05:00:00Z',
    completed_at: '2026-07-20T05:00:03Z',
    executor: {},
    capabilities: {},
    project_revision: {
      project_id: 'project-real',
      revision: 3,
      fingerprint: 'a'.repeat(64),
      observed_at: '2026-07-20T05:00:00Z',
    },
    correlation_id: 'correlation-0123456789abcdef',
    causation_id: null,
    last_activity_sequence: 4,
    checkpoint_id: null,
    error: status === 'failed'
      ? { error: { code: 'provider_timeout', message: '模型响应超时', retryable } }
      : null,
  }
}

describe('Inspector', () => {
  afterEach(cleanup)

  it('refreshes both the writing service and the Agent provider state', async () => {
    const user = userEvent.setup()
    const onRefreshRuntime = vi.fn().mockResolvedValue(undefined)
    const refreshConversation = vi.fn()

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={onRefreshRuntime}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, refresh: refreshConversation }}
    />)

    await user.click(screen.getByRole('button', { name: '刷新写作助手' }))

    expect(refreshConversation).toHaveBeenCalledTimes(1)
    expect(onRefreshRuntime).toHaveBeenCalledTimes(1)
  })

  it('starts a new conversation and lets the author reopen saved conversation history', async () => {
    const user = userEvent.setup()
    const createConversation = vi.fn().mockResolvedValue(undefined)
    const selectConversation = vi.fn().mockResolvedValue(undefined)

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 002 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{
        ...conversation,
        availability: 'available',
        sessions: [currentSession, olderSession],
        session: currentSession,
        createConversation,
        selectConversation,
      }}
    />)

    expect(screen.getByText(/系统会自动整理相关资料/)).toBeInTheDocument()
    expect(screen.getByText(/勾选“发送前看资料”可以先预览/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '查看历史对话' }))
    expect(screen.getByRole('region', { name: '历史对话' })).toBeInTheDocument()
    expect(screen.getByText('修改第二章开头')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /查找人物伏笔/ }))
    expect(selectConversation).toHaveBeenCalledWith(olderSession.session_id)

    await user.click(screen.getByRole('button', { name: '新建对话' }))
    expect(createConversation).toHaveBeenCalledTimes(1)
  })

  it('explains the missing model in plain language and opens settings in place', async () => {
    const user = userEvent.setup()
    const onOpenSettings = vi.fn()

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={conversation}
      onOpenSettings={onOpenSettings}
    />)

    expect(screen.getByText('还没配置写作模型')).toBeInTheDocument()
    expect(screen.getByText('先配置接口地址、密钥和模型，写作助手才能开始工作。')).toBeInTheDocument()
    expect(screen.queryByText('Writing Agent provider is not configured')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '配置写作模型' }))
    expect(onOpenSettings).toHaveBeenCalledTimes(1)
  })

  it('surfaces a persisted candidate for author review without authorizing formal prose', async () => {
    const user = userEvent.setup()
    const activeRun = recoveryRun('succeeded', false)
    const candidate = await agentCandidateOutputFixture()
    const onReviewCandidate = vi.fn()

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available', activeRun }}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
      onReviewCandidate={onReviewCandidate}
    />)

    expect(screen.getByText('修改结果待你确认')).toBeInTheDocument()
    expect(screen.getByText('修复方案 · 写前修复方案')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开结果' }))
    expect(onReviewCandidate).toHaveBeenCalledTimes(1)
    expect(screen.queryByText('可以开始写作对话')).not.toBeInTheDocument()
  })

  it('shows a pending Provider recovery dossier and submits only an explicit author decision', async () => {
    const user = userEvent.setup()
    const activeRun = recoveryRun('interrupted', false)
    const workflow = agentWorkflowStateFixture({
      runId: activeRun.run_id,
      runStatus: 'interrupted',
      providerBoundary: true,
    })
    const candidate = await agentCandidateOutputFixture()
    const response = await providerRecoveryFixture()
    const decide = vi.fn().mockResolvedValue(true)
    const refresh = vi.fn()
    const compileContext = vi.fn().mockResolvedValue(undefined)

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available', activeRun, workflow, refresh, compileContext }}
      candidateOutput={{ output: candidate, state: 'ready', error: null, refresh: vi.fn() }}
      providerRecovery={{
        response,
        state: 'ready',
        action: null,
        error: null,
        actionError: null,
        canAdopt: true,
        refresh: vi.fn(),
        decide,
      }}
    />)

    expect(screen.getByText('Provider 结果待人工决策')).toBeInTheDocument()
    expect(screen.getByText('未知')).toBeInTheDocument()
    expect(screen.getByText('persisted')).toBeInTheDocument()
    expect(screen.getByText('provider_replay_count=0')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重新编译新 Run' })).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: /采纳已保存候选/ }))
    await waitFor(() => expect(decide).toHaveBeenCalledWith('adopt_persisted_candidate'))
    expect(refresh).toHaveBeenCalledTimes(1)
    expect(compileContext).not.toHaveBeenCalled()
  })

  it('removes Gate fixtures when a demo workspace becomes a real project', async () => {
    const user = userEvent.setup()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      chapterLabel: '第 001 章',
      projectId: null,
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation,
    }
    const view = render(<Inspector {...props} demo />)

    await user.click(screen.getByRole('tab', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: /写作检查/ }))
    expect(screen.getByText('可继续生成候选')).toBeInTheDocument()

    view.rerender(<Inspector {...props} demo={false} projectId="project-real" />)

    expect(screen.queryByText('可继续生成候选')).not.toBeInTheDocument()
  })

  it('removes Reader fixtures when a demo workspace becomes a real project', async () => {
    const user = userEvent.setup()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      chapterLabel: '第 001 章',
      projectId: null,
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation,
    }
    const view = render(<Inspector {...props} demo />)

    await user.click(screen.getByRole('tab', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: /审稿反馈/ }))
    expect(screen.getByText('严格冷读')).toBeInTheDocument()

    view.rerender(<Inspector {...props} demo={false} projectId="project-real" />)

    expect(screen.queryByText('严格冷读')).not.toBeInTheDocument()
  })

  it('drops a source batch filter immediately when the active project changes', async () => {
    const user = userEvent.setup()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-a',
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation,
    }
    const view = render(<Inspector {...props} />)

    await user.click(screen.getByRole('tab', { name: '资料' }))
    await user.click(screen.getByRole('button', { name: '打开测试资料批次' }))
    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent(
      'project-a|source-import-project-a',
    )

    view.rerender(<Inspector {...props} projectId="project-b" />)

    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent('project-b|all')
    expect(screen.getByTestId('knowledge-scope')).not.toHaveTextContent(
      'source-import-project-a',
    )
  })

  it('labels Context compilation as the first step and opens the pending Pack for review', async () => {
    const user = userEvent.setup()
    const compileContext = vi.fn().mockResolvedValue(undefined)
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation: { ...conversation, availability: 'available' as const, compileContext },
    }
    const view = render(<Inspector {...props} />)

    await user.click(screen.getByRole('checkbox', { name: '发送前看资料' }))
    await user.type(screen.getByLabelText('给写作助手的指令'), '收紧开头')
    await user.click(screen.getByRole('button', { name: '发送给写作助手' }))
    expect(compileContext).toHaveBeenCalledWith('收紧开头')

    const route = nlclRouteFixture()
    view.rerender(<Inspector {...props} conversation={{
      ...props.conversation,
      pendingContextPack: contextPackFixture(),
      pendingNlclRoute: route,
      pendingPrewriteOperationPackage: prewriteOperationPackageFixture(route, {
        readiness: 'risk_draft_only',
        missing_requirements: [],
      }),
      contextState: 'ready',
    }} />)

    expect(screen.getByRole('tab', { name: '对话' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByRole('button', { name: '确认并生成候选' })).toBeEnabled()
    expect(screen.getByText('实际发送给模型的消息')).toBeInTheDocument()
  })

  it('keeps an explicit chapter edit in the author-review prose path', async () => {
    const user = userEvent.setup()
    const onPrepareInstruction = vi.fn().mockResolvedValue(undefined)
    const confirmPendingContext = vi.fn().mockResolvedValue(undefined)
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
      onPrepareInstruction,
      conversation: {
        ...conversation,
        availability: 'available' as const,
        confirmPendingContext,
      },
    }
    const view = render(<Inspector {...props} />)

    await user.type(
      screen.getByLabelText('给写作助手的指令'),
      '把第二章开头改写得更紧一点',
    )
    await user.click(screen.getByRole('button', { name: '发送给写作助手' }))
    expect(onPrepareInstruction).toHaveBeenCalledWith('把第二章开头改写得更紧一点')

    const route = nlclRouteFixture()
    view.rerender(<Inspector {...props} conversation={{
      ...props.conversation,
      pendingContextPack: contextPackFixture({ objective: '把第二章开头改写得更紧一点' }),
      pendingNlclRoute: route,
      pendingPrewriteOperationPackage: prewriteOperationPackageFixture(route, {
        readiness: 'risk_draft_only',
        missing_requirements: [],
      }),
      contextState: 'ready',
    }} />)

    await waitFor(() => {
      expect(confirmPendingContext).toHaveBeenCalledWith('author_review_change_proposal')
    })
    expect(screen.queryByText('实际发送给模型的消息')).not.toBeInTheDocument()
  })

  it('routes a next-chapter chat command through the existing draft service', async () => {
    const user = userEvent.setup()
    const onPrepareInstruction = vi.fn().mockResolvedValue(undefined)
    const onDraftNextChapter = vi.fn().mockResolvedValue(undefined)
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available' }}
      onPrepareInstruction={onPrepareInstruction}
      onDraftNextChapter={onDraftNextChapter}
    />)

    await user.type(screen.getByLabelText('给写作助手的指令'), '继续写下一章')
    await user.click(screen.getByRole('button', { name: '发送给写作助手' }))

    expect(onDraftNextChapter).toHaveBeenCalledOnce()
    expect(onPrepareInstruction).not.toHaveBeenCalled()
  })

  it('does not reinterpret a batch-writing command as an edit to the open chapter', async () => {
    const user = userEvent.setup()
    const onPrepareInstruction = vi.fn().mockResolvedValue(undefined)
    const onDraftNextChapter = vi.fn().mockResolvedValue(undefined)
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available' }}
      onPrepareInstruction={onPrepareInstruction}
      onDraftNextChapter={onDraftNextChapter}
    />)

    await user.type(screen.getByLabelText('给写作助手的指令'), '一键写10章')
    await user.click(screen.getByRole('button', { name: '发送给写作助手' }))

    expect(screen.getByRole('alert')).toHaveTextContent(
      '一次写 10 章需要可暂停、可恢复的批量任务队列',
    )
    expect(onPrepareInstruction).not.toHaveBeenCalled()
    expect(onDraftNextChapter).not.toHaveBeenCalled()
  })

  it('auto-opens a pending Context Pack once and lets the author switch back to knowledge', async () => {
    const user = userEvent.setup()
    const pendingContextPack = contextPackFixture()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation: {
        ...conversation,
        pendingContextPack,
      },
    }
    const view = render(<Inspector {...props} />)

    await waitFor(() => {
      expect(screen.getByText('实际发送给模型的消息')).toBeInTheDocument()
    })
    await user.click(screen.getByRole('tab', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: /写作知识/ }))
    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent('project-real|all')

    view.rerender(<Inspector
      {...props}
      conversation={{
        ...props.conversation,
        pendingContextPack: contextPackFixture(),
      }}
    />)

    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent('project-real|all')
  })

  it('does not steal the knowledge tab when a pending Context Pack arrives mid-review', async () => {
    const user = userEvent.setup()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation,
    }
    const view = render(<Inspector {...props} />)

    await user.click(screen.getByRole('tab', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: /写作知识/ }))
    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent('project-real|all')

    view.rerender(<Inspector
      {...props}
      conversation={{
        ...conversation,
        pendingContextPack: contextPackFixture(),
      }}
    />)

    expect(screen.getByTestId('knowledge-scope')).toHaveTextContent('project-real|all')
    expect(screen.queryByText('实际发送给模型的消息')).not.toBeInTheDocument()
  })

  it('carries the explicit knowledge-to-writing intent into Run creation', async () => {
    const user = userEvent.setup()
    const compileContext = vi.fn().mockResolvedValue(undefined)
    const confirmPendingContext = vi.fn().mockResolvedValue(undefined)
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
      conversation: {
        ...conversation,
        availability: 'available' as const,
        compileContext,
        confirmPendingContext,
      },
    }
    const view = render(<Inspector {...props} />)

    await user.click(screen.getByRole('tab', { name: '更多' }))
    await user.click(screen.getByRole('button', { name: /写作知识/ }))
    await user.click(screen.getByRole('button', { name: '使用这批知识开始写作' }))
    expect(compileContext).toHaveBeenCalledWith(
      '使用本批已正式化知识，为当前章节生成可供作者逐项审阅的正文修改建议。',
      'chapter',
      formalKnowledgeSelector,
    )
    expect(screen.getByRole('tab', { name: '对话' })).toHaveAttribute('aria-selected', 'true')

    const route = nlclRouteFixture()
    view.rerender(<Inspector {...props} conversation={{
      ...props.conversation,
      pendingContextPack: contextPackFixture(),
      pendingNlclRoute: route,
      pendingPrewriteOperationPackage: prewriteOperationPackageFixture(route, {
        readiness: 'risk_draft_only',
        missing_requirements: [],
      }),
      contextState: 'ready',
    }} />)

    expect(screen.getByRole('radio', { name: '正文候选' })).toHaveAttribute('aria-checked', 'true')
    await user.click(screen.getByRole('button', { name: '确认并生成候选' }))
    expect(confirmPendingContext).toHaveBeenCalledWith('author_review_change_proposal')
  })

  it('summarizes succeeded blocked route execution in author language while retaining technical status', () => {
    const route = nlclRouteFixture()
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{
        ...conversation,
        activeNlclRoute: route,
        activeNlclRouteExecutions: [
          nlclRouteExecutionFixture(route, {
            call_ordinal: 1,
            status: 'succeeded',
            receipt: nlclRouteExecutionReceiptFixture(route, { call_ordinal: 1, domain_status: 'healthy' }),
          }),
          nlclRouteExecutionFixture(route, {
            call_ordinal: 2,
            status: 'succeeded',
            receipt: nlclRouteExecutionReceiptFixture(route, { call_ordinal: 2, domain_status: 'degraded' }),
          }),
          nlclRouteExecutionFixture(route, {
            call_ordinal: 3,
            status: 'succeeded',
            receipt: nlclRouteExecutionReceiptFixture(route, { call_ordinal: 3, domain_status: 'blocked' }),
          }),
        ],
      }}
    />)

    expect(screen.getByText('写作准备 · 准备写作指令已完成')).toBeInTheDocument()
    expect(screen.getByText('已经生成待审结果，正文仍要由你确认后才会改动。')).toBeInTheDocument()
    expect(screen.getByText('技术详情')).toBeInTheDocument()
    expect(screen.getByText('execution_status')).toBeInTheDocument()
    expect(screen.getByText('domain_status')).toBeInTheDocument()
    expect(screen.getByText('blocked')).toBeInTheDocument()
    expect(screen.queryByText('最终领域状态：blocked')).not.toBeInTheDocument()
  })

  it('shows the active running route ordinal before the waiting final ordinal', () => {
    const route = nlclRouteFixture()
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{
        ...conversation,
        activeNlclRoute: route,
        activeNlclRouteExecutions: [
          nlclRouteExecutionFixture(route, {
            call_ordinal: 1,
            status: 'running',
            run_id: 'run-0123456789abcdef0123456789',
            tool_call_id: 'tool-audit-0123456789abcdef',
          }),
          nlclRouteExecutionFixture(route, {
            call_ordinal: 2,
            status: 'awaiting_execution',
          }),
        ],
      }}
    />)

    expect(screen.getByText('写作准备 · 核对作品资料进行中')).toBeInTheDocument()
    expect(screen.getByText('系统正在核对作品资料，完成后会把结果交给你确认。')).toBeInTheDocument()
  })

  it('shows failed and cancelled route executions as author decisions rather than raw statuses', () => {
    const route = nlclRouteFixture()
    const props = {
      runtime,
      onRefreshRuntime: vi.fn().mockResolvedValue(undefined),
      demo: false,
      chapterLabel: '第 001 章',
      projectId: 'project-real',
      projectError: null,
      onRefreshProject: vi.fn(),
    }
    const view = render(<Inspector
      {...props}
      conversation={{
        ...conversation,
        activeNlclRoute: route,
        activeNlclRouteExecutions: [
          nlclRouteExecutionFixture(route, {
            call_ordinal: 1,
            status: 'failed',
            error_code: 'skill_timeout',
            error_message: '写前 Skill 超时',
          }),
        ],
      }}
    />)

    expect(screen.getByText('写作准备 · 核对作品资料失败')).toBeInTheDocument()
    expect(screen.getByText('写前 Skill 超时')).toBeInTheDocument()

    view.rerender(<Inspector
      {...props}
      conversation={{
        ...conversation,
        activeNlclRoute: route,
        activeNlclRouteExecutions: [
          nlclRouteExecutionFixture(route, {
            call_ordinal: 1,
            status: 'cancelled',
            error_message: '作者取消了本次写前链',
          }),
        ],
      }}
    />)

    expect(screen.getByText('写作准备 · 核对作品资料已取消')).toBeInTheDocument()
    expect(screen.getByText('作者取消了本次写前链')).toBeInTheDocument()
  })

  it('opens the controlled ChangeSet tab with the exact proposal target', () => {
    const preferredChangeSetId = 'changeset-0123456789abcdef0123456789abcdef'
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={conversation}
      activeTab="changes"
      onTabChange={vi.fn()}
      preferredChangeSetId={preferredChangeSetId}
    />)

    expect(screen.getByRole('tab', { name: '改稿' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByTestId('changeset-target')).toHaveTextContent(preferredChangeSetId)
  })

  it('recompiles a retryable failed run with its original objective', async () => {
    const user = userEvent.setup()
    const compileContext = vi.fn().mockResolvedValue(undefined)
    const activeRun = recoveryRun('failed', true)

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available', activeRun, compileContext }}
    />)

    expect(screen.getByText('上次任务失败')).toBeInTheDocument()
    expect(screen.getByText('模型响应超时')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新准备' }))
    expect(compileContext).toHaveBeenCalledWith(activeRun.objective)
  })

  it('allows an interrupted run to be recompiled', async () => {
    const user = userEvent.setup()
    const compileContext = vi.fn().mockResolvedValue(undefined)
    const activeRun = recoveryRun('interrupted', false)

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available', activeRun, compileContext }}
    />)

    expect(screen.getByText('上次任务已中断')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重新准备' }))
    expect(compileContext).toHaveBeenCalledWith(activeRun.objective)
  })

  it('shows paused workflow progress and resumes explicitly without a stop control', async () => {
    const user = userEvent.setup()
    const activeRun = recoveryRun('paused', false)
    const resume = vi.fn().mockResolvedValue(undefined)
    const workflow = agentWorkflowStateFixture({
      runId: activeRun.run_id,
      completedSteps: 2,
    })

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{
        ...conversation,
        availability: 'available',
        activeRun,
        workflow,
        running: false,
        busy: true,
        resume,
      }}
    />)

    expect(screen.getByText('本次任务已暂停')).toBeInTheDocument()
    expect(screen.getByText('已完成 2/3 步 · 下一步：第 3 步 · 准备写作指令')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '停止生成' })).not.toBeInTheDocument()
    expect(screen.getByLabelText('给写作助手的指令')).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '继续任务' }))
    expect(resume).toHaveBeenCalledTimes(1)
  })

  it('shows Provider dispatch as the next action after all three workflow steps', async () => {
    const user = userEvent.setup()
    const activeRun = recoveryRun('paused', false)
    const resume = vi.fn().mockResolvedValue(undefined)
    const workflow = agentWorkflowStateFixture({
      runId: activeRun.run_id,
      completedSteps: 3,
    })

    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{
        ...conversation,
        availability: 'available',
        activeRun,
        workflow,
        running: false,
        busy: true,
        resume,
      }}
    />)

    expect(screen.getByText('已完成 3/3 步 · 下一步：调用模型写作')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '继续任务' }))
    expect(resume).toHaveBeenCalledTimes(1)
  })

  it.each(['interrupted', 'failed', 'cancelled'] as const)(
    'never offers Resume after the Provider boundary for a %s Run and creates a new Run instead',
    async (terminalStatus) => {
      const user = userEvent.setup()
      const activeRun = recoveryRun(terminalStatus, terminalStatus === 'failed')
      const compileContext = vi.fn().mockResolvedValue(undefined)
      const resume = vi.fn().mockResolvedValue(undefined)
      const workflow = agentWorkflowStateFixture({
        runId: activeRun.run_id,
        runStatus: terminalStatus,
        providerBoundary: true,
      })

      render(<Inspector
        runtime={runtime}
        onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
        demo={false}
        chapterLabel="第 001 章"
        projectId="project-real"
        projectError={null}
        onRefreshProject={vi.fn()}
        conversation={{
          ...conversation,
          availability: 'available',
          activeRun,
          workflow,
          compileContext,
          resume,
        }}
      />)

      expect(screen.getByText('模型返回状态不确定')).toBeInTheDocument()
      expect(screen.getByText(/已经完成 3\/3 步。为避免重复写作，系统不会自动重发/)).toBeInTheDocument()
      expect(screen.queryByText('上次任务失败')).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: '继续任务' })).not.toBeInTheDocument()
      expect(resume).not.toHaveBeenCalled()
      await user.click(screen.getByRole('button', { name: '重新开始这次任务' }))
      expect(compileContext).toHaveBeenCalledWith(activeRun.objective)
    },
  )

  it('does not offer recompilation for a non-retryable failed run', () => {
    render(<Inspector
      runtime={runtime}
      onRefreshRuntime={vi.fn().mockResolvedValue(undefined)}
      demo={false}
      chapterLabel="第 001 章"
      projectId="project-real"
      projectError={null}
      onRefreshProject={vi.fn()}
      conversation={{ ...conversation, availability: 'available', activeRun: recoveryRun('failed', false) }}
    />)

    expect(screen.getByText('上次任务失败')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '重新准备' })).not.toBeInTheDocument()
  })
})

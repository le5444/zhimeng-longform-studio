import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useContextHandoff, type ContextHandoffState } from '../hooks/useContextHandoff'
import { contextHandoffRecordFixture } from '../test/contextHandoffFixture'
import { ContextHandoffPanel } from './ContextHandoffPanel'

vi.mock('../hooks/useContextHandoff', () => ({
  useContextHandoff: vi.fn(),
}))

function state(overrides: Partial<ContextHandoffState> = {}): ContextHandoffState {
  return {
    state: 'ready',
    record: contextHandoffRecordFixture(),
    busy: false,
    error: null,
    refresh: vi.fn(async () => undefined),
    prepare: vi.fn(async () => undefined),
    approve: vi.fn(async () => undefined),
    reject: vi.fn(async () => undefined),
    disable: vi.fn(async () => undefined),
    run: vi.fn(async () => undefined),
    ...overrides,
  }
}

describe('ContextHandoffPanel', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('shows the author-facing O2-D to Context Pack relation and folds raw evidence', () => {
    vi.mocked(useContextHandoff).mockReturnValue(state())

    render(
      <ContextHandoffPanel
        enabled
        projectId="project-o2e"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    expect(screen.getByRole('region', { name: '受控入口到 Context Pack' })).toBeInTheDocument()
    expect(screen.getByText('O2-D distillation_index')).toBeInTheDocument()
    expect(screen.getByText(/import-distillation-index → assemble-context/)).toBeInTheDocument()
    expect(screen.getByText('Runtime Context Pack')).toBeInTheDocument()
    expect(screen.getByText('知识入口已受控')).toBeInTheDocument()
    expect(screen.getByText('只生成写前上下文')).toBeInTheDocument()
    expect(screen.getByText(/route_health=ready 只说明可调用，不等于真实写作验证/)).toBeInTheDocument()
    const technicalDetails = screen.getByText('技术详情 / receipt / Context Pack').closest('details')
    expect(technicalDetails).not.toHaveAttribute('open')
    expect(technicalDetails).toHaveTextContent('5'.repeat(64))
  })

  it('wires route inputs and author decision actions to the hook', () => {
    const current = state()
    vi.mocked(useContextHandoff).mockReturnValue(current)

    render(
      <ContextHandoffPanel
        enabled
        projectId="project-o2e"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    fireEvent.change(screen.getByRole('textbox', { name: '上下文档位' }), { target: { value: 'review' } })
    fireEvent.change(screen.getByRole('textbox', { name: '任务阶段' }), { target: { value: 'rewrite' } })
    fireEvent.change(screen.getByRole('textbox', { name: '章节' }), { target: { value: 'CH009' } })
    fireEvent.change(screen.getByRole('textbox', { name: '任务意图' }), { target: { value: '装配返工上下文；不写正文。' } })
    fireEvent.click(screen.getByRole('button', { name: '准备 Context Pack' }))
    fireEvent.click(screen.getByRole('button', { name: '批准' }))
    fireEvent.click(screen.getByRole('button', { name: '拒绝' }))
    fireEvent.click(screen.getByRole('button', { name: '禁用' }))

    expect(current.prepare).toHaveBeenCalledWith({
      profile: 'review',
      stage: 'rewrite',
      chapterId: 'CH009',
      taskIntent: '装配返工上下文；不写正文。',
    })
    expect(current.approve).toHaveBeenCalled()
    expect(current.reject).toHaveBeenCalled()
    expect(current.disable).toHaveBeenCalled()
  })

  it('renders successful persistence without claiming prose writeback', () => {
    vi.mocked(useContextHandoff).mockReturnValue(state({
      record: contextHandoffRecordFixture({
        status: 'succeeded',
        context_pack_id: `context-pack-o2e-${'1'.repeat(32)}`,
        context_pack_hash: '2'.repeat(64),
        route_receipt_id: `nlcl-route-${'3'.repeat(32)}`,
        route_receipt_hash: '4'.repeat(64),
        result: {
          status: 'context_ready',
          route_health: 'ready',
          profile: 'chapter',
          stage: 'draft',
          chapter_id: 'CH001',
          selected_entries: contextHandoffRecordFixture().upstream?.entries,
        },
      }),
    }))

    render(
      <ContextHandoffPanel
        enabled
        projectId="project-o2e"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    expect(screen.getByText('Context Pack 已生成')).toBeInTheDocument()
    expect(screen.getByText('Context Pack 已生成，正文尚未写回')).toBeInTheDocument()
    expect(screen.getByText(/完整 id\/hash 在技术详情中可复制/)).toBeInTheDocument()
    expect(screen.queryByText(/正文已写回/)).not.toBeInTheDocument()
  })
})

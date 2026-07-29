import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { adjacentChainRecordFixture } from '../test/adjacentChainFixture'
import { useAdjacentChain, type AdjacentChainState } from '../hooks/useAdjacentChain'
import { AdjacentChainPanel } from './AdjacentChainPanel'

vi.mock('../hooks/useAdjacentChain', () => ({
  useAdjacentChain: vi.fn(),
}))

function state(overrides: Partial<AdjacentChainState> = {}): AdjacentChainState {
  return {
    state: 'ready',
    record: adjacentChainRecordFixture(),
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

describe('AdjacentChainPanel', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('shows the continuous O2-D to O2-E author path and step cards', () => {
    vi.mocked(useAdjacentChain).mockReturnValue(state())

    render(
      <AdjacentChainPanel
        enabled
        projectId="project-adjacent"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={`source-import-${'d'.repeat(32)}`}
      />,
    )

    expect(screen.getByRole('region', { name: '相邻 Skill 连续链' })).toBeInTheDocument()
    expect(screen.getByText('O2-D → O2-E 相邻连续链')).toBeInTheDocument()
    expect(screen.getByText('O2-D')).toBeInTheDocument()
    expect(screen.getByText('单次风险批准')).toBeInTheDocument()
    expect(screen.getByText('O2-E')).toBeInTheDocument()
    expect(screen.getByText('相邻链已准备')).toBeInTheDocument()
    expect(screen.getByText('O2-D distillation -> KB handoff')).toBeInTheDocument()
    expect(screen.getByText('O2-E KB index -> Context Pack')).toBeInTheDocument()
    expect(screen.getAllByText(/固定两步相邻链|风险审批绑定|formal_draft_allowed/).length).toBeGreaterThan(0)
  })

  it('wires prepare, approve, reject, disable and run actions to the hook', () => {
    const current = state({ record: adjacentChainRecordFixture({ status: 'prepared' }) })
    vi.mocked(useAdjacentChain).mockReturnValue(current)

    const { rerender } = render(
      <AdjacentChainPanel
        enabled
        projectId="project-adjacent"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={`source-import-${'d'.repeat(32)}`}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /准备连续链/ }))
    fireEvent.click(screen.getByRole('button', { name: /批准一次/ }))
    fireEvent.click(screen.getByRole('button', { name: /拒绝/ }))
    fireEvent.click(screen.getByRole('button', { name: /禁用/ }))
    expect(current.prepare).toHaveBeenCalledWith(`source-import-${'d'.repeat(32)}`)
    expect(current.approve).toHaveBeenCalled()
    expect(current.reject).toHaveBeenCalled()
    expect(current.disable).toHaveBeenCalled()

    const approved = state({ record: adjacentChainRecordFixture({ status: 'approved' }) })
    vi.mocked(useAdjacentChain).mockReturnValue(approved)
    rerender(
      <AdjacentChainPanel
        enabled
        projectId="project-adjacent"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={null}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /连续运行 O2-D→O2-E/ }))
    expect(approved.run).toHaveBeenCalled()
  })

  it('renders completed chain as context pack assembly, not formal draft success', () => {
    vi.mocked(useAdjacentChain).mockReturnValue(state({
      record: adjacentChainRecordFixture({
        status: 'succeeded',
        context_pack_id: 'context-pack-o2e-fixture',
        context_pack_hash: '9'.repeat(64),
        route_receipt_id: `nlcl-route-${'c'.repeat(32)}`,
        steps: [
          {
            step_id: 'o2d-kb-handoff',
            ordinal: 1,
            title: 'O2-D distillation -> KB handoff',
            skill_id: 'novel-distillation.kb-handoff',
            capability: 'import-distillation-index',
            status: 'succeeded',
            child_id: `skill-handoff-${'b'.repeat(32)}`,
            result_hash: '1'.repeat(64),
            error_code: null,
            error_message: null,
          },
          {
            step_id: 'o2e-context-handoff',
            ordinal: 2,
            title: 'O2-E KB index -> Context Pack',
            skill_id: 'novel-kb.context-handoff',
            capability: 'assemble-context',
            status: 'succeeded',
            child_id: `context-handoff-${'e'.repeat(32)}`,
            result_hash: '2'.repeat(64),
            error_code: null,
            error_message: null,
          },
        ],
      }),
    }))

    render(
      <AdjacentChainPanel
        enabled
        projectId="project-adjacent"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    expect(screen.getByText('连续交接完成')).toBeInTheDocument()
    expect(screen.getByText('Context Pack 已装配')).toBeInTheDocument()
    expect(screen.getAllByText(/context-pack-o2e-fixture/).length).toBeGreaterThan(0)
    expect(screen.getByText(/formal_draft_allowed 仍为 false/)).toBeInTheDocument()
    expect(screen.queryByText(/正式正文已写回|正式正文授权/)).not.toBeInTheDocument()
  })

  it('offers author-controlled Context review handoff after success', () => {
    const onOpenContextReview = vi.fn(async () => undefined)
    vi.mocked(useAdjacentChain).mockReturnValue(state({
      record: adjacentChainRecordFixture({
        status: 'succeeded',
        context_pack_id: 'context-pack-o2e-fixture',
        context_pack_hash: '9'.repeat(64),
        route_receipt_id: `nlcl-route-${'c'.repeat(32)}`,
      }),
    }))

    render(
      <AdjacentChainPanel
        enabled
        projectId="project-adjacent"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        onOpenContextReview={onOpenContextReview}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /打开 Context 审查/ }))
    expect(onOpenContextReview).toHaveBeenCalledWith('context-pack-o2e-fixture')
  })
})

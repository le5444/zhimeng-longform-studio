import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { skillHandoffRecordFixture } from '../test/skillHandoffFixture'
import { useSkillHandoff, type SkillHandoffState } from '../hooks/useSkillHandoff'
import { SkillHandoffPanel } from './SkillHandoffPanel'

vi.mock('../hooks/useSkillHandoff', () => ({
  useSkillHandoff: vi.fn(),
}))

function state(overrides: Partial<SkillHandoffState> = {}): SkillHandoffState {
  return {
    state: 'ready',
    record: skillHandoffRecordFixture(),
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

describe('SkillHandoffPanel', () => {
  afterEach(() => cleanup())

  beforeEach(() => {
    vi.resetAllMocks()
  })

  it('shows the author-facing upstream-to-downstream relation and hides raw receipts behind details', () => {
    vi.mocked(useSkillHandoff).mockReturnValue(state())

    render(
      <SkillHandoffPanel
        enabled
        projectId="project-o2d"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={`source-import-${'d'.repeat(32)}`}
      />,
    )

    expect(screen.getByRole('region', { name: '原始 Skill 相邻交接' })).toBeInTheDocument()
    expect(screen.getByText('novel-distillation')).toBeInTheDocument()
    expect(screen.getByText('Runtime 受管 adapter')).toBeInTheDocument()
    expect(screen.getByText('novel-kb-manager')).toBeInTheDocument()
    expect(screen.getByText('待作者批准')).toBeInTheDocument()
    expect(screen.getByText('作品机制')).toBeInTheDocument()
    expect(screen.getByText('叙事声音')).toBeInTheDocument()
    expect(screen.getByText('正文校验')).toBeInTheDocument()
    expect(screen.getByText('只读项目快照；只写 Runtime staging；无网络；无 secrets。')).toBeInTheDocument()
    expect(screen.getByText('技术详情 / receipt / checkpoint')).toBeInTheDocument()
  })

  it('wires prepare, approve, reject, disable and run actions to the hook', () => {
    const current = state({ record: skillHandoffRecordFixture({ status: 'prepared' }) })
    vi.mocked(useSkillHandoff).mockReturnValue(current)

    const { rerender } = render(
      <SkillHandoffPanel
        enabled
        projectId="project-o2d"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={`source-import-${'d'.repeat(32)}`}
      />,
    )

    fireEvent.click(screen.getByRole('button', { name: /准备交接/ }))
    fireEvent.click(screen.getByRole('button', { name: /批准/ }))
    fireEvent.click(screen.getByRole('button', { name: /拒绝/ }))
    fireEvent.click(screen.getByRole('button', { name: /禁用/ }))
    expect(current.prepare).toHaveBeenCalledWith(`source-import-${'d'.repeat(32)}`)
    expect(current.approve).toHaveBeenCalled()
    expect(current.reject).toHaveBeenCalled()
    expect(current.disable).toHaveBeenCalled()

    const approved = state({ record: skillHandoffRecordFixture({ status: 'approved' }) })
    vi.mocked(useSkillHandoff).mockReturnValue(approved)
    rerender(
      <SkillHandoffPanel
        enabled
        projectId="project-o2d"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
        sourceImportId={null}
      />,
    )
    fireEvent.click(screen.getByRole('button', { name: /运行交接/ }))
    expect(approved.run).toHaveBeenCalled()
  })

  it('renders completed handoff as a controlled entry rather than a writeback success', () => {
    vi.mocked(useSkillHandoff).mockReturnValue(state({
      record: skillHandoffRecordFixture({
        status: 'succeeded',
        skill_receipt_id: `skill-receipt-${'1'.repeat(32)}`,
        skill_receipt_hash: '2'.repeat(64),
        result: {
          schema: 'zhimeng.novel-kb-handoff-result.v1',
          status: 'ready_for_downstream',
          distillation_index_hash: '3'.repeat(64),
          downstream_target: 'novel-kb-manager.import-distillation-index',
          counts: { work: 1, voice: 1, validator: 1 },
          risk_summary: '不复制卡全文。',
          next_action: '等待下一相邻边消费。',
        },
      }),
    }))

    render(
      <SkillHandoffPanel
        enabled
        projectId="project-o2d"
        projectRevision={3}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    expect(screen.getByText('交接产物已生成')).toBeInTheDocument()
    expect(screen.getByText('受控入口已生成')).toBeInTheDocument()
    expect(screen.getByText(/novel-kb-manager\.import-distillation-index/)).toBeInTheDocument()
    expect(screen.getByText('3333333333333333333333333333333333333333333333333333333333333333')).toBeInTheDocument()
    expect(screen.queryByText(/正文已写回/)).not.toBeInTheDocument()
  })
})

import { StrictMode } from 'react'
import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  fetchDistillationHealth,
  previewDistillationCoverage,
} from '../api/distillation'
import {
  distillationCoverageFixture,
  distillationHealthFixture,
} from '../test/distillationFixture'
import { DistillationCoveragePanel } from './DistillationCoveragePanel'

vi.mock('../api/distillation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/distillation')>()
  return {
    ...actual,
    fetchDistillationHealth: vi.fn(),
    previewDistillationCoverage: vi.fn(),
  }
})

describe('DistillationCoveragePanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(fetchDistillationHealth).mockResolvedValue(distillationHealthFixture)
    vi.mocked(previewDistillationCoverage).mockResolvedValue(distillationCoverageFixture)
  })

  it('shows degraded library health without claiming the library is healthy', async () => {
    render(<DistillationCoveragePanel />)

    expect(await screen.findByText('风险就绪 · 1,124 张卡')).toBeInTheDocument()
    expect(screen.queryByText('健康 · 1,124 张卡')).not.toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '写作目标' })).toHaveAttribute('maxlength', '1000')
  })

  it('distinguishes failed validation maturity from risk-ready debt', async () => {
    vi.mocked(fetchDistillationHealth).mockResolvedValue({
      ...distillationHealthFixture,
      validation_maturity: 'degraded',
    })
    render(<DistillationCoveragePanel />)

    expect(await screen.findByText('验证退化 · 1,124 张卡')).toBeInTheDocument()
    expect(screen.queryByText('风险就绪 · 1,124 张卡')).not.toBeInTheDocument()
  })

  it('renders source and formal coverage as a read-only risk preview', async () => {
    const user = userEvent.setup()
    render(<DistillationCoveragePanel />)

    await screen.findByText('风险就绪 · 1,124 张卡')
    await user.type(
      screen.getByRole('textbox', { name: '写作目标' }),
      distillationCoverageFixture.query,
    )
    await user.click(screen.getByRole('button', { name: '检查覆盖' }))

    await waitFor(() => expect(previewDistillationCoverage).toHaveBeenCalledWith(
      distillationCoverageFixture.query,
      'draft_assist',
    ))
    expect(await screen.findByRole('status')).toHaveTextContent('只能作为风险预览，不进入正式写作')
    expect(screen.getByText('100%')).toBeInTheDocument()
    expect(screen.getByText('40%')).toBeInTheDocument()
    expect(screen.getByText('系统流公共评价类型合同')).toBeInTheDocument()
    expect(screen.getByText('开篇兑现风险闸门')).toBeInTheDocument()
    expect(screen.queryByText(/已注入|正式上下文已装配/)).not.toBeInTheDocument()
  })

  it('does not call the Skill while Runtime is offline', () => {
    render(<DistillationCoveragePanel enabled={false} />)

    expect(screen.getByText('Writing Runtime 未就绪')).toBeInTheDocument()
    expect(fetchDistillationHealth).not.toHaveBeenCalled()
    expect(previewDistillationCoverage).not.toHaveBeenCalled()
  })

  it('prevents a second query while the synchronous Skill call is running', async () => {
    const user = userEvent.setup()
    vi.mocked(previewDistillationCoverage).mockReturnValue(new Promise(() => {}))
    render(<DistillationCoveragePanel />)

    await screen.findByText('风险就绪 · 1,124 张卡')
    const query = screen.getByRole('textbox', { name: '写作目标' })
    await user.type(query, '第一章 爽点兑现')
    await user.click(screen.getByRole('button', { name: '检查覆盖' }))

    expect(query).toBeDisabled()
    expect(screen.getByRole('combobox', { name: '任务类型' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '检查中' })).toBeDisabled()
    expect(previewDistillationCoverage).toHaveBeenCalledTimes(1)
  })

  it('deduplicates the automatic health check under React StrictMode', async () => {
    render(<StrictMode><DistillationCoveragePanel /></StrictMode>)

    expect(await screen.findByText('风险就绪 · 1,124 张卡')).toBeInTheDocument()
    expect(fetchDistillationHealth).toHaveBeenCalledTimes(1)
  })

  it('does not start coverage before the health request settles', async () => {
    vi.mocked(fetchDistillationHealth).mockReturnValue(new Promise(() => {}))
    render(<DistillationCoveragePanel />)

    const query = screen.getByRole('textbox', { name: '写作目标' })
    expect(query).toBeDisabled()
    expect(screen.getByRole('combobox', { name: '任务类型' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '检查覆盖' })).toBeDisabled()
    expect(previewDistillationCoverage).not.toHaveBeenCalled()
  })
})

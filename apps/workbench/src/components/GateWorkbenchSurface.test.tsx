import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { GateWorkbenchSurface } from './GateWorkbenchSurface'

vi.mock('./GatePipelinePanel', () => ({
  GatePipelinePanel: () => <div>Gate Pipeline Panel Stub</div>,
}))
vi.mock('./LiteraryQualityGatePanel', () => ({
  LiteraryQualityGatePanel: () => <div>文学质量 Gate Panel Stub</div>,
}))
vi.mock('./LlmLiteraryScorePanel', () => ({
  LlmLiteraryScorePanel: () => <div>LLM 形分 Panel Stub</div>,
}))
vi.mock('./ExternalBlindReviewMatrixPanel', () => ({
  ExternalBlindReviewMatrixPanel: () => <div>外部盲评矩阵 Panel Stub</div>,
}))
vi.mock('./HardFactGatePanel', () => ({
  HardFactGatePanel: () => <div>硬事实 Gate Panel Stub</div>,
}))
vi.mock('./FormalDraftAuthorizationPanel', () => ({
  FormalDraftAuthorizationPanel: () => <div>正式起稿授权 Panel Stub</div>,
}))

describe('GateWorkbenchSurface', () => {
  it('shows stage rail and focuses one gate panel without formal unlock claims', async () => {
    const user = userEvent.setup()
    render(
      <GateWorkbenchSurface
        enabled
        projectId="project-1"
        changeSetId="changeset-1"
        reviewRevision={1}
        changeSetStatus="applied"
      />,
    )

    expect(screen.getByLabelText('Gate 工作面')).toBeInTheDocument()
    expect(screen.getByText(/formal_draft_allowed=false/)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: /Pipeline/i })).toBeInTheDocument()
    expect(screen.getByText('Gate Pipeline Panel Stub')).toBeInTheDocument()
    expect(screen.queryByText('文学质量 Gate Panel Stub')).not.toBeInTheDocument()

    await user.click(screen.getByRole('tab', { name: /文学质量/i }))
    expect(screen.getByText('文学质量 Gate Panel Stub')).toBeInTheDocument()
    expect(screen.queryByText('Gate Pipeline Panel Stub')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '全部' }))
    expect(screen.getByText('Gate Pipeline Panel Stub')).toBeInTheDocument()
    expect(screen.getByText('文学质量 Gate Panel Stub')).toBeInTheDocument()
    expect(screen.getByText('LLM 形分 Panel Stub')).toBeInTheDocument()
    expect(screen.getByText('外部盲评矩阵 Panel Stub')).toBeInTheDocument()
    expect(screen.getByText('硬事实 Gate Panel Stub')).toBeInTheDocument()
    expect(screen.getByText('正式起稿授权 Panel Stub')).toBeInTheDocument()
  })
})

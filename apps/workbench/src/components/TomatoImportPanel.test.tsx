import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getKnowledgeSourceFormalization } from '../api/knowledge'
import { getDistillationInvocation } from '../api/distillation'
import {
  cancelSourceImport,
  createLocalSourceImport,
  createSourceImport,
  getSourceImportExecutionReceipt,
  inspectLocalSource,
  listSourceImports,
  prepareSourceImportDistillationInvocation,
  retrySourceImport,
  searchTomatoNovels,
} from '../api/sourceImports'
import { runDistillationInvocation } from '../api/distillation'
import { RuntimeApiError } from '../api/runtime'
import {
  localEpubSourceCandidateFixture,
  localSourceImportJobFixture,
  sourceImportFormalizationFixture,
  sourceImportJobFixture,
  sourceImportReceiptFixture,
  tomatoCandidateFixture,
} from '../test/sourceImportFixture'
import {
  distillationInvocationSnapshotFixture,
  runtimeDistillationInvocationId,
} from '../test/distillationInvocationFixture'
import { TomatoImportPanel } from './TomatoImportPanel'

vi.mock('../api/sourceImports', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/sourceImports')>()
  return {
    ...actual,
    cancelSourceImport: vi.fn(),
    createLocalSourceImport: vi.fn(),
    createSourceImport: vi.fn(),
    getSourceImportExecutionReceipt: vi.fn(),
    inspectLocalSource: vi.fn(),
    listSourceImports: vi.fn(),
    prepareSourceImportDistillationInvocation: vi.fn(),
    retrySourceImport: vi.fn(),
    searchTomatoNovels: vi.fn(),
  }
})

vi.mock('../api/knowledge', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/knowledge')>()
  return {
    ...actual,
    getKnowledgeSourceFormalization: vi.fn(),
  }
})

vi.mock('../api/distillation', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api/distillation')>()
  return {
    ...actual,
    getDistillationInvocation: vi.fn(),
    runDistillationInvocation: vi.fn(),
  }
})

describe('TomatoImportPanel', () => {
  afterEach(cleanup)

  beforeEach(() => {
    vi.resetAllMocks()
    vi.mocked(listSourceImports).mockResolvedValue([])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture(),
    )
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture(),
    )
    vi.mocked(getDistillationInvocation).mockResolvedValue(
      distillationInvocationSnapshotFixture(),
    )
  })

  it('shows same-name candidates and starts the selected import', async () => {
    const user = userEvent.setup()
    const second = {
      ...tomatoCandidateFixture,
      index: 2,
      book_id: '22223333',
      author: '同名作者乙',
    }
    vi.mocked(searchTomatoNovels).mockResolvedValue({
      schema: 'zhimeng.tomato-search.v1',
      query: tomatoCandidateFixture.title,
      resolution: 'selection_required',
      candidates: [tomatoCandidateFixture, second],
      selected_candidate: null,
    })
    vi.mocked(createSourceImport).mockResolvedValue(
      sourceImportJobFixture({ candidate: second }),
    )

    render(<TomatoImportPanel />)
    await user.type(screen.getByRole('searchbox', { name: '番茄小说' }), tomatoCandidateFixture.title)
    await user.click(screen.getByRole('button', { name: '下载并蒸馏' }))

    expect(await screen.findByText('选择同名作品')).toBeInTheDocument()
    expect(screen.getByText(/同名作者乙 · ID 22223333/)).toBeInTheDocument()
    expect(createSourceImport).not.toHaveBeenCalled()

    await user.click(screen.getByRole('button', { name: /选择《长夜巡灯人》，作者 同名作者乙/ }))
    await waitFor(() => expect(createSourceImport).toHaveBeenCalledWith(
      tomatoCandidateFixture.title,
      second,
    ))
    expect(await screen.findByText('导入任务已创建')).toBeInTheDocument()
  })

  it('checks a local EPUB identity before creating an auditable import', async () => {
    const user = userEvent.setup()
    const created = localSourceImportJobFixture({ candidate: localEpubSourceCandidateFixture })
    vi.mocked(inspectLocalSource).mockResolvedValue(localEpubSourceCandidateFixture)
    vi.mocked(createLocalSourceImport).mockResolvedValue(created)

    render(<TomatoImportPanel />)
    await user.click(screen.getByRole('button', { name: '本地 TXT / EPUB' }))
    await user.type(
      screen.getByRole('textbox', { name: '本地 TXT / EPUB' }),
      localEpubSourceCandidateFixture.source_path,
    )
    await user.click(screen.getByRole('button', { name: '校验并蒸馏' }))

    await waitFor(() => expect(inspectLocalSource).toHaveBeenCalledWith(
      localEpubSourceCandidateFixture.source_path,
    ))
    expect(createLocalSourceImport).toHaveBeenCalledWith(localEpubSourceCandidateFixture)
    expect(await screen.findByText('本地导入任务已创建')).toBeInTheDocument()
    expect(screen.getAllByText(localEpubSourceCandidateFixture.title).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText(localEpubSourceCandidateFixture.source_sha256)).toBeInTheDocument()
    expect(screen.getByText(/EPUB · unknown/)).toBeInTheDocument()
    expect(screen.getByRole('textbox', { name: '本地 TXT / EPUB' })).toHaveValue('')
  })

  it('labels a draft as awaiting review rather than formally imported', async () => {
    const awaiting = sourceImportJobFixture({ status: 'awaiting_review', percent: 100 })
    vi.mocked(listSourceImports).mockResolvedValue([awaiting])

    render(<TomatoImportPanel />)

    expect((await screen.findAllByText('蒸馏草案待复核')).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('8 张候选卡，尚未进入正式知识库')).toBeInTheDocument()
    expect(screen.getByText('完整源包')).toBeInTheDocument()
    expect(screen.getByText('856,240 字')).toBeInTheDocument()
    expect(screen.queryByText('已正式入库')).not.toBeInTheDocument()
    expect(screen.queryByText(/undefined%/)).not.toBeInTheDocument()
  })

  it('shows the verified source execution identity while a draft awaits review', async () => {
    const awaiting = sourceImportJobFixture({ status: 'awaiting_review' })
    const sourceReceipt = sourceImportReceiptFixture({ importId: awaiting.import_id })
    vi.mocked(listSourceImports).mockResolvedValue([awaiting])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(sourceReceipt)
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: awaiting.import_id }),
    )

    render(<TomatoImportPanel />)

    const evidence = await screen.findByRole('region', { name: '执行与正式化收据' })
    await waitFor(() => expect(within(evidence).getAllByText('verified')).toHaveLength(2))
    expect(within(evidence).getByText(`receipt_id · ${sourceReceipt.receipt?.receipt_id}`)).toBeInTheDocument()
    expect(within(evidence).getByText(`receipt_hash · ${sourceReceipt.receipt?.receipt_hash}`)).toBeInTheDocument()
    expect(within(evidence).getByText(`run_id · ${sourceReceipt.receipt?.run_id}`)).toBeInTheDocument()
    expect(within(evidence).getByText('revision')).toBeInTheDocument()
    expect(within(evidence).getByText(`idempotency_key · ${sourceReceipt.receipt?.idempotency_key}`)).toBeInTheDocument()
    expect(within(evidence).getByText('awaiting_review')).toBeInTheDocument()
    expect(screen.getByText('仍是旧 Source Import Skill 草案')).toBeInTheDocument()
    expect(getDistillationInvocation).not.toHaveBeenCalled()
  })

  it('shows the Runtime-owned deep distillation ledger when linked from the source receipt', async () => {
    const awaiting = sourceImportJobFixture({ status: 'awaiting_review' })
    const sourceReceipt = sourceImportReceiptFixture({
      importId: awaiting.import_id,
      distillationInvocationId: runtimeDistillationInvocationId,
    })
    vi.mocked(listSourceImports).mockResolvedValue([awaiting])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(sourceReceipt)
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: awaiting.import_id }),
    )
    vi.mocked(getDistillationInvocation).mockResolvedValue(
      distillationInvocationSnapshotFixture(),
    )

    render(<TomatoImportPanel />)

    await screen.findByText('候选已持久化')
    const deepInvocation = screen.getByRole('region', {
      name: 'Runtime 深蒸馏 Invocation',
    })
    expect(within(deepInvocation).getByText('候选已持久化')).toBeInTheDocument()
    expect(within(deepInvocation).getByText('4 条')).toBeInTheDocument()
    expect(within(deepInvocation).getByText('1 turn')).toBeInTheDocument()
    expect(within(deepInvocation).getByText('1/1/1')).toBeInTheDocument()
    expect(within(deepInvocation).getByText('0 次')).toBeInTheDocument()
    expect(within(deepInvocation).getByText(`invocation_id · ${runtimeDistillationInvocationId}`)).toBeInTheDocument()
    expect(within(deepInvocation).getByText(/candidate_output_receipt_id · distillation-receipt-/)).toBeInTheDocument()
    expect(getDistillationInvocation).toHaveBeenCalledWith(
      runtimeDistillationInvocationId,
      {},
      expect.any(AbortSignal),
    )
  })

  it('prepares a legacy source import into a Runtime-owned invocation and runs Provider deep distillation', async () => {
    const user = userEvent.setup()
    const awaiting = sourceImportJobFixture({ status: 'awaiting_review' })
    const sourceReceipt = sourceImportReceiptFixture({ importId: awaiting.import_id })
    const preparedSnapshot = distillationInvocationSnapshotFixture({ status: 'prepared' })
    const runSnapshot = distillationInvocationSnapshotFixture({ status: 'output_persisted' })
    const anchorCatalog = {
      schema: 'zhimeng.distillation-anchor-catalog.v1',
      invocation_id: runtimeDistillationInvocationId,
      anchors: [],
    }
    const textByAnchorId = {
      [`distillation-anchor-${'a'.repeat(32)}`]: '正文锚点',
    }
    vi.mocked(listSourceImports).mockResolvedValue([awaiting])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(sourceReceipt)
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: awaiting.import_id }),
    )
    vi.mocked(getDistillationInvocation).mockResolvedValue(preparedSnapshot)
    vi.mocked(prepareSourceImportDistillationInvocation).mockResolvedValue({
      schema: 'zhimeng.source-import-distillation-prepare-result.v1',
      snapshot: preparedSnapshot,
      anchor_catalog: anchorCatalog,
      text_by_anchor_id: textByAnchorId,
    })
    vi.mocked(runDistillationInvocation).mockResolvedValue({
      ...runSnapshot,
      terminal_reason: 'distillation_output_persisted',
    })

    render(
      <TomatoImportPanel
        projectId={`project-${'8'.repeat(24)}`}
        projectRevision={7}
        projectFingerprint={'f'.repeat(64)}
      />,
    )

    const legacyEvidence = await screen.findByRole('region', {
      name: 'Runtime 深蒸馏 Invocation',
    })
    expect(within(legacyEvidence).getByText('仍是旧 Source Import Skill 草案')).toBeInTheDocument()
    await user.click(within(legacyEvidence).getByRole('button', { name: '准备 Runtime 深蒸馏' }))

    await waitFor(() => expect(prepareSourceImportDistillationInvocation).toHaveBeenCalledWith(
      awaiting.import_id,
      {
        project_id: `project-${'8'.repeat(24)}`,
        expected_project_revision: 7,
        expected_project_fingerprint: 'f'.repeat(64),
      },
      { timeoutMs: 610_000 },
    ))
    expect(await screen.findByText('已准备')).toBeInTheDocument()
    expect(screen.getByText(`invocation_id · ${runtimeDistillationInvocationId}`)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '运行 Provider 深蒸馏' }))
    await waitFor(() => expect(runDistillationInvocation).toHaveBeenCalledWith(
      runtimeDistillationInvocationId,
      {
        anchor_catalog: anchorCatalog,
        text_by_anchor_id: textByAnchorId,
      },
      { timeoutMs: 610_000 },
    ))
    expect(await screen.findByText('候选已持久化')).toBeInTheDocument()
  })

  it('confirms completed imports only with both verified receipts and opens the knowledge batch', async () => {
    const user = userEvent.setup()
    const completed = sourceImportJobFixture({ status: 'completed' })
    const sourceReceipt = sourceImportReceiptFixture({ importId: completed.import_id })
    const formalization = sourceImportFormalizationFixture({
      importId: completed.import_id,
      status: 'verified',
    })
    const onReviewDraft = vi.fn()
    vi.mocked(listSourceImports).mockResolvedValue([completed])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(sourceReceipt)
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(formalization)

    render(<TomatoImportPanel onReviewDraft={onReviewDraft} />)

    expect(await screen.findByText('正式蒸馏已验证入库')).toBeInTheDocument()
    const evidence = screen.getByRole('region', { name: '执行与正式化收据' })
    expect(within(evidence).getByText(`formalization_receipt_id · ${formalization.receipt?.formalization_receipt_id}`)).toBeInTheDocument()
    expect(within(evidence).getByText(`formalization_receipt_hash · ${formalization.receipt?.receipt_hash}`)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '查看知识批次' }))
    expect(onReviewDraft).toHaveBeenCalledWith(completed.import_id)
  })

  it('marks a receipt-less completed history as legacy_unverified', async () => {
    const completed = sourceImportJobFixture({ status: 'completed' })
    vi.mocked(listSourceImports).mockResolvedValue([completed])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture({
        importId: completed.import_id,
        lineageStatus: 'legacy_unverified',
      }),
    )
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({
        importId: completed.import_id,
        status: 'legacy_unverified',
      }),
    )

    render(<TomatoImportPanel />)

    expect((await screen.findAllByText('legacy_unverified')).length).toBeGreaterThanOrEqual(1)
    expect(screen.getByText('缺少可验证收据，不能把流程完成状态当作正式入库证明。')).toBeInTheDocument()
    expect(screen.queryByText('正式蒸馏已验证入库')).not.toBeInTheDocument()
  })

  it('renders nullable progress without a fabricated percentage', async () => {
    const queued = sourceImportJobFixture({
      status: 'queued',
      percent: null,
      savedChapters: null,
      totalChapters: null,
    })
    vi.mocked(listSourceImports).mockResolvedValue([queued])

    render(<TomatoImportPanel />)

    expect(await screen.findByText('Fixture queued')).toBeInTheDocument()
    expect(screen.queryByText(/undefined|null%|NaN%/)).not.toBeInTheDocument()
  })

  it('exposes cancel for active jobs and retry only for terminal failures', async () => {
    const user = userEvent.setup()
    const running = sourceImportJobFixture({ status: 'downloading' })
    const cancelRequested = sourceImportJobFixture({ status: 'cancel_requested' })
    vi.mocked(listSourceImports).mockResolvedValue([running])
    vi.mocked(cancelSourceImport).mockResolvedValue(cancelRequested)
    const first = render(<TomatoImportPanel />)

    await user.click(await screen.findByRole('button', { name: '取消任务' }))
    await waitFor(() => expect(cancelSourceImport).toHaveBeenCalledWith(running.import_id))
    first.unmount()

    const failed = sourceImportJobFixture({ status: 'failed', error: '下载失败' })
    const retried = sourceImportJobFixture({
      importId: 'source-import-fedcba9876543210fedcba9876543210',
      retryOf: failed.import_id,
    })
    vi.mocked(listSourceImports).mockResolvedValue([failed])
    vi.mocked(retrySourceImport).mockResolvedValue(retried)
    render(<TomatoImportPanel />)

    expect(await screen.findByText('下载失败')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '重试任务' }))
    await waitFor(() => expect(retrySourceImport).toHaveBeenCalledWith(failed.import_id))
  })

  it('keeps historical jobs and receipts readable when search adapter is unavailable', async () => {
    const user = userEvent.setup()
    const historical = sourceImportJobFixture({ status: 'awaiting_review' })
    vi.mocked(listSourceImports).mockResolvedValue([historical])
    vi.mocked(getSourceImportExecutionReceipt).mockResolvedValue(
      sourceImportReceiptFixture({ importId: historical.import_id }),
    )
    vi.mocked(getKnowledgeSourceFormalization).mockResolvedValue(
      sourceImportFormalizationFixture({ importId: historical.import_id }),
    )
    vi.mocked(searchTomatoNovels).mockRejectedValue(
      new RuntimeApiError(503, { detail: 'Tomato Skill adapter is not configured' }),
    )
    const first = render(<TomatoImportPanel />)

    expect(await screen.findByText('完整源包')).toBeInTheDocument()
    await user.type(screen.getByRole('searchbox', { name: '番茄小说' }), '无法检索的新书')
    await user.click(screen.getByRole('button', { name: '下载并蒸馏' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Tomato Skill adapter is not configured')
    expect(screen.getByText('完整源包')).toBeInTheDocument()
    expect(getSourceImportExecutionReceipt).toHaveBeenCalledWith(historical.import_id)
    first.unmount()

    vi.clearAllMocks()
    render(<TomatoImportPanel enabled={false} />)
    expect(screen.getByText('Writing Runtime 未就绪')).toBeInTheDocument()
    expect(listSourceImports).not.toHaveBeenCalled()
  })
})

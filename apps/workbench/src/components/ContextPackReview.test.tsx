import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import {
  contextPackFixture,
  nativeKnowledgeFragmentFixture,
} from '../test/contextPackFixture'
import {
  nlclRouteExecutionFixture,
  nlclRouteExecutionReceiptFixture,
  nlclRouteFixture,
  prewriteOperationPackageFixture,
} from '../test/nlclRouteFixture'
import { ContextPackReview } from './ContextPackReview'

describe('ContextPackReview', () => {
  afterEach(cleanup)

  it('shows selection trace, dual budgets, exact Provider messages, and all integrity hashes', async () => {
    const user = userEvent.setup()
    const onConfirm = vi.fn().mockResolvedValue(undefined)
    const route = nlclRouteFixture()
    const operationPackage = prewriteOperationPackageFixture(route, {
      readiness: 'risk_draft_only',
      missing_requirements: [],
    })
    const record = contextPackFixture({
      excluded: [{
        source_id: 'risk-card-one',
        source_type: 'knowledge_card',
        title: '风险片段',
        authority: 'risk',
        version: 'candidate-v1',
        source_hash: '4'.repeat(64),
        priority: 90,
        mandatory: false,
        eligible: true,
        candidate_exclusion_reason: null,
        fragment_hash: '5'.repeat(64),
        estimated_characters: 300,
        estimated_tokens: 120,
        exclusion_reason: 'risk_authority',
      }],
      gates: [{
        code: 'risk_fragment_excluded',
        severity: 'warning',
        message: '风险卡不会进入正式上下文',
        source_id: 'risk-card-one',
      }],
      readiness: 'degraded',
    })

    render(<ContextPackReview
      record={record}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="degraded"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={onConfirm}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)

    expect(screen.getByText('项目控制卡')).toBeInTheDocument()
    expect(screen.getByText('风险片段')).toBeInTheDocument()
    expect(screen.getByText('风险卡不会进入正式上下文')).toBeInTheDocument()
    expect(screen.getByLabelText('字符预算')).toHaveAttribute('value', '1200')
    expect(screen.getByLabelText('Token 预算')).toHaveAttribute('value', '500')
    expect(screen.getByText('项目控制卡：必须保留黄铜钥匙。')).toBeInTheDocument()
    expect(screen.getAllByText('1'.repeat(64)).length).toBeGreaterThan(0)
    expect(screen.getByText('2'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('3'.repeat(64))).toBeInTheDocument()
    expect(screen.getByLabelText('作者运行结论')).toHaveTextContent('知识已绑定')
    expect(screen.getByLabelText('作者运行结论')).toHaveTextContent('正文尚未写回')
    expect(screen.getByLabelText('项目知识绑定')).toHaveTextContent('身份已绑定')
    expect(screen.getByLabelText('项目知识绑定')).toHaveTextContent(record.pack.project_kb_state_id ?? '')
    expect(screen.getByRole('button', { name: '复制pack hash' })).toBeInTheDocument()

    const readiness = screen.getByLabelText('写前链就绪')
    expect(readiness).toHaveTextContent('写前链就绪 · 可确认运行')
    expect(readiness).toHaveTextContent('项目审计 → 写前门禁 → 创作交接')
    expect(readiness).toHaveTextContent('formal_draft_allowed=false')
    expect(readiness).toHaveTextContent('通过 · NLCL 路由')
    expect(readiness).toHaveTextContent('通过 · 写前运行包')
    expect(screen.queryByLabelText('确认阻断原因')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '确认并运行' }))
    expect(onConfirm).toHaveBeenCalledOnce()
  })

  it('keeps legacy bindings readable and mismatched bindings blocked', () => {
    const legacyRecord = contextPackFixture({
      project_kb_state_id: null,
      project_kb_manifest_hash: null,
    })
    const legacyRoute = nlclRouteFixture({
      project_kb_state_id: null,
      project_kb_manifest_hash: null,
    })
    const view = render(<ContextPackReview
      record={legacyRecord}
      nlclRoute={legacyRoute}
      operationPackage={prewriteOperationPackageFixture(legacyRoute)}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)

    expect(screen.getByLabelText('项目知识绑定')).toHaveTextContent('旧记录 · 仅可查看')
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()

    const mismatchedRoute = nlclRouteFixture({
      project_kb_manifest_hash: 'f'.repeat(64),
    })
    view.rerender(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={mismatchedRoute}
      operationPackage={prewriteOperationPackageFixture(mismatchedRoute)}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)
    expect(screen.getByLabelText('项目知识绑定')).toHaveTextContent('绑定不匹配')
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()
  })

  it('shows native knowledge provenance and keeps legacy fragments without provenance readable', () => {
    const nativeFragment = nativeKnowledgeFragmentFixture()
    const legacyFragment = nativeKnowledgeFragmentFixture({
      source_id: 'knowledge-card:legacy-work-card',
      title: '旧正式知识卡',
      version: `knowledge-version-${'9'.repeat(32)}`,
      source_hash: '9'.repeat(64),
      knowledge_provenance: undefined,
    })

    render(<ContextPackReview
      record={contextPackFixture({ included: [nativeFragment, legacyFragment] })}
      nlclRoute={nlclRouteFixture()}
      operationPackage={prewriteOperationPackageFixture(nlclRouteFixture())}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)

    const provenance = screen.getByLabelText('阶段压力兑现 知识来源链')
    expect(provenance).toHaveTextContent(`source_import_id · source-import-${'a'.repeat(32)}`)
    expect(provenance).toHaveTextContent(`candidate_id · knowledge-candidate-${'c'.repeat(32)}`)
    expect(provenance).toHaveTextContent(`version_id · knowledge-version-${'d'.repeat(32)}`)
    expect(provenance).toHaveTextContent(`version_lineage_receipt_id · knowledge-lineage-${'e'.repeat(32)}`)
    expect(provenance).toHaveTextContent('formalization_receipt_id · knowledge-formalization-7889af1170bbe5bf1a0120df7b724cd4')
    expect(provenance).toHaveTextContent(`formalization_receipt_hash · ${'7'.repeat(64)}`)
    expect(screen.getByText('旧正式知识卡')).toBeInTheDocument()
    expect(screen.queryByLabelText('旧正式知识卡 知识来源链')).not.toBeInTheDocument()
  })

  it('requires an explicit Writer mode and keeps prose output review-only', async () => {
    const user = userEvent.setup()
    const onWriterModeChange = vi.fn()
    const route = nlclRouteFixture()
    const operationPackage = prewriteOperationPackageFixture(route, {
      readiness: 'risk_draft_only',
      missing_requirements: [],
    })
    const view = render(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      writerMode="repair_only"
      onWriterModeChange={onWriterModeChange}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)

    expect(screen.getByRole('radio', { name: '诊断修复' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('只生成修复方案，不创建正文改动')).toBeInTheDocument()
    await user.click(screen.getByRole('radio', { name: '正文候选' }))
    expect(onWriterModeChange).toHaveBeenCalledWith('author_review_change_proposal')

    view.rerender(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      writerMode="author_review_change_proposal"
      onWriterModeChange={onWriterModeChange}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)
    expect(screen.getByRole('radio', { name: '正文候选' })).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText('生成正文改动候选，仍需作者审阅后写回')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '确认并生成候选' })).toBeEnabled()
  })

  it('prevents confirmation for blocked or stale Packs and keeps refresh available', async () => {
    const user = userEvent.setup()
    const onRefresh = vi.fn().mockResolvedValue(undefined)
    const route = nlclRouteFixture()
    const operationPackage = prewriteOperationPackageFixture(route, {
      readiness: 'risk_draft_only',
      missing_requirements: [],
    })
    const view = render(<ContextPackReview
      record={contextPackFixture({ readiness: 'blocked' })}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="blocked"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)

    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()
    expect(screen.getByLabelText('写前链就绪')).toHaveTextContent('写前链未就绪 · 确认已锁定')
    expect(screen.getByLabelText('确认阻断原因')).toHaveTextContent('Context Pack')

    view.rerender(<ContextPackReview
      record={contextPackFixture({ readiness: 'ready' })}
      nlclRoute={route}
      operationPackage={prewriteOperationPackageFixture(route, {
        readiness: 'blocked',
        missing_requirements: ['current_character_state_unbound'],
      })}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeEnabled()

    view.rerender(<ContextPackReview
      record={contextPackFixture({ readiness: 'ready' })}
      nlclRoute={route}
      operationPackage={prewriteOperationPackageFixture(route, {
        readiness: 'blocked',
        missing_requirements: ['migrated_prewrite_package_requires_recompile'],
      })}
      state="blocked"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()

    view.rerender(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="stale"
      staleReasons={['当前章节已经变化']}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)
    expect(screen.getByRole('alert', { name: 'Context Pack 已过期' })).toHaveTextContent('当前章节已经变化')
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: '重新编译' }))
    expect(onRefresh).toHaveBeenCalledOnce()

    view.rerender(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="ready"
      staleReasons={[]}
      agentAvailable={false}
      agentDetail="Writing Agent provider is not configured"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)
    expect(screen.getByRole('status')).toHaveTextContent('Writing Agent provider is not configured')
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()

    view.rerender(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={route}
      operationPackage={operationPackage}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive
      error="当前 Run 尚未结束"
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={onRefresh}
    />)
    expect(screen.getByRole('status')).toHaveTextContent('当前 Run 尚未结束')
    expect(screen.getByRole('alert')).toHaveTextContent('当前 Run 尚未结束')
    expect(screen.getByRole('button', { name: '重新编译' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '确认并运行' })).toBeDisabled()
  })

  it('shows the immutable NLCL route, selected cards, next Creation call, and draft boundary', () => {
    render(<ContextPackReview
      record={contextPackFixture()}
      nlclRoute={nlclRouteFixture()}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      onConfirm={vi.fn().mockResolvedValue(undefined)}
      onDiscard={vi.fn()}
      onRefresh={vi.fn().mockResolvedValue(undefined)}
    />)

    expect(screen.getByText('写作上下文路线')).toBeInTheDocument()
    expect(screen.getByText('本项目正式知识')).toBeInTheDocument()
    expect(screen.getByText('阶段压力兑现')).toBeInTheDocument()
    expect(screen.getByLabelText('写前链就绪')).toHaveTextContent('项目审计 → 写前门禁 → 创作交接')
    expect(screen.getByText(/novel-creation-suite\.project-audit/)).toBeInTheDocument()
    expect(screen.getByText(/novel-creation-suite\.prewrite-gate/)).toBeInTheDocument()
    expect(screen.getByText(/novel-creation-suite\.creation-handoff/)).toBeInTheDocument()
    expect(screen.getAllByText('等待 Runtime 执行')).toHaveLength(3)
    expect(screen.getByRole('note')).toHaveTextContent('正式正文不会在此步骤自动写回')
    expect(screen.getByLabelText('写前链就绪')).toHaveTextContent('写前链未就绪 · 确认已锁定')
    expect(screen.getByLabelText('确认阻断原因')).toHaveTextContent('写前运行包')
    expect(screen.getAllByText('6'.repeat(64)).length).toBeGreaterThan(0)
  })

  it('shows the durable route execution receipt and domain blocker without action buttons', () => {
    const route = nlclRouteFixture()
    const receipt = nlclRouteExecutionReceiptFixture(route)
    const prewriteReceipt = nlclRouteExecutionReceiptFixture(route, {
      call_ordinal: 2,
      receipt_hash: 'c'.repeat(64),
    })
    render(<ContextPackReview
      record={{ ...contextPackFixture(), consumed_by_run_id: receipt.run_id }}
      nlclRoute={route}
      routeExecutions={[
        nlclRouteExecutionFixture(route, {
          status: 'succeeded',
          run_id: receipt.run_id,
          tool_call_id: receipt.tool_call_id,
          receipt,
        }),
        nlclRouteExecutionFixture(route, {
          call_ordinal: 2,
          status: 'succeeded',
          run_id: prewriteReceipt.run_id,
          tool_call_id: prewriteReceipt.tool_call_id,
          receipt: prewriteReceipt,
        }),
      ]}
      operationPackage={prewriteOperationPackageFixture(route)}
      state="ready"
      staleReasons={[]}
      agentAvailable
      agentDetail="ready"
      runActive={false}
      error={null}
      readOnly
    />)

    expect(screen.getAllByRole('status')).toHaveLength(2)
    expect(screen.getAllByRole('status')[0]).toHaveTextContent('项目审计已完成，正文仍未写回')
    expect(screen.getAllByRole('status')[1]).toHaveTextContent('仅允许生成待审候选')
    expect(screen.getByText('写作前检查包')).toBeInTheDocument()
    expect(screen.getAllByText('scene_contract_unbound')).toHaveLength(2)
    expect(screen.getByText('b'.repeat(64))).toBeInTheDocument()
    expect(screen.getByText('c'.repeat(64))).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '确认并运行' })).not.toBeInTheDocument()
  })
})

/**
 * 右栏「修改」（9-29 回炉重造）：这个文件夹里的每一次修改，新的在上；点一条看改了哪几行；
 * 没保存的能保存（先把每一处都接受，再写进文件）/ 不要，保存过的能撤回，都先问一句。
 * review 整个用假的（它是 hooks/useChangeSets 的返回值），不启动服务。
 */
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ChangeHunk, ChangeSet, ChangeSetSummary } from '../../api/changeSets'
import type { useChangeSets } from '../../hooks/useChangeSets'
import { ChangesPanel } from '../panel/Changes'
import { A } from '../text'
import type { ConfirmRequest } from '../ui'
import { makeSnapshot } from './fixtures'

type Review = ReturnType<typeof useChangeSets>
const C = A.changes

afterEach(cleanup)

const PROSE_PATH = '05_正文内容/第001卷_起/001-050章/001_山门将倾.md'

function summary(over: Partial<ChangeSetSummary>): ChangeSetSummary {
  return {
    change_set_id: 'cs-1',
    project_id: 'p1',
    title: '',
    origin: 'agent',
    source_run_id: null,
    source_tool_call_id: null,
    gate_required: false,
    status: 'applied',
    base_revision: 1,
    base_fingerprint: 'f',
    review_revision: 2,
    file_count: 1,
    hunk_count: 1,
    pending_hunk_count: 0,
    accepted_hunk_count: 1,
    rejected_hunk_count: 0,
    created_at: '2026-09-13T12:00:00Z',
    updated_at: '2026-09-13T12:00:00Z',
    ...over,
  }
}

function hunk(over: Partial<ChangeHunk>): ChangeHunk {
  return {
    hunk_id: 'h1',
    index: 0,
    before_start: 0,
    before_end: 1,
    after_start: 0,
    after_end: 1,
    before_text: '旧的一句。',
    after_text: '新的一句。',
    before_sha256: 'a',
    after_sha256: 'b',
    decision: 'pending',
    ...over,
  }
}

function detail(over: Partial<ChangeSet>, hunks: ChangeHunk[] = [hunk({ decision: 'accepted' })]): ChangeSet {
  return {
    schema: 'zhimeng.change-set.v1',
    change_set_id: 'cs-1',
    project_id: 'p1',
    base_revision: 1,
    base_fingerprint: 'f',
    title: '',
    summary: '',
    origin: 'agent',
    source_run_id: null,
    source_tool_call_id: null,
    source_request_hash: null,
    source_skill_ids: [],
    gate_required: false,
    ai_smell: null,
    status: 'applied',
    review_revision: 2,
    changes: [{
      change_id: 'c1',
      operation: 'update',
      file_id: 'f1',
      relative_path: PROSE_PATH,
      rationale: '',
      before_sha256: null,
      proposed_after_sha256: null,
      preserve_utf8_bom: false,
      post_draft_gate_scope: 'prose_body',
      hunks,
    }],
    writeback: null,
    rollback: null,
    error: null,
    created_at: '2026-09-13T12:00:00Z',
    updated_at: '2026-09-13T12:00:00Z',
    ...over,
  } as unknown as ChangeSet
}

function makeReview(over: Partial<Review> = {}): Review {
  return {
    records: [],
    listState: 'ready',
    listError: null,
    selectedId: null,
    detail: null,
    detailState: 'idle',
    detailError: null,
    action: null,
    actionError: null,
    actionConflict: false,
    select: vi.fn(),
    decide: vi.fn(),
    decideMany: vi.fn(async () => undefined),
    editHunk: vi.fn(),
    apply: vi.fn(async () => undefined),
    rollback: vi.fn(async () => undefined),
    discardByIds: vi.fn(async () => undefined),
    refresh: vi.fn(),
    ...over,
  } as unknown as Review
}

function renderPanel(review: Review, over: Partial<Parameters<typeof ChangesPanel>[0]> = {}) {
  const props = { snapshot: makeSnapshot(), review, focusId: null, onRefreshProject: vi.fn(), notify: vi.fn(), askConfirm: vi.fn(), ...over }
  const view = render(<ChangesPanel {...props} />)
  return { ...props, rerender: (next: Review) => view.rerender(<ChangesPanel {...props} review={next} />) }
}

describe('右栏「修改」', () => {
  it('退回列表后再次请求同一修改仍重新打开详情', () => {
    const review = makeReview({ records: [summary({})], selectedId: 'cs-1', detailState: 'ready', detail: detail({}) })
    const props = { snapshot: makeSnapshot(), review, focusId: 'cs-1', focusRequest: 1, onRefreshProject: vi.fn(), notify: vi.fn(), askConfirm: vi.fn() }
    const view = render(<ChangesPanel {...props} />)
    fireEvent.click(screen.getByRole('button', { name: C.back }))
    expect(document.querySelector('.zm-diff-lines')).toBeNull()
    view.rerender(<ChangesPanel {...props} focusRequest={2} />)
    expect(document.querySelector('.zm-diff-lines')).not.toBeNull()
    expect(review.select).toHaveBeenCalledTimes(2)
  })

  it('打开先刷一遍；新的在上，谁改的写在前面，Agent 自己起的题目另起一行；点一条看它', () => {
    const review = makeReview({
      records: [
        summary({ change_set_id: 'cs-old', origin: 'manual', title: '作者修改 第 001 章 山门将倾', created_at: '2026-09-12T08:00:00Z' }),
        summary({ change_set_id: 'cs-new', origin: 'agent', title: '重写第1章开头段落，增强画面感', created_at: '2026-09-13T08:00:00Z' }),
      ],
    })
    renderPanel(review)
    expect(review.refresh).toHaveBeenCalledTimes(1)
    const titles = Array.from(document.querySelectorAll('.zm-change-row-title')).map((node) => node.textContent)
    // 列表里只有标题和文件数：说几个文件，Agent 自己起的题目另起一行（不再把章号翻成章名）。
    expect(titles).toEqual(['Agent 改了 1 个文件', '你改了 1 个文件'])
    expect(screen.getByText('重写第1章开头段落，增强画面感')).toBeInTheDocument()
    fireEvent.click(screen.getByText('你改了 1 个文件'))
    expect(review.select).toHaveBeenCalledWith('cs-old')
  })

  it('还没有修改就照实说', () => {
    renderPanel(makeReview())
    expect(screen.getByText(C.empty)).toBeInTheDocument()
  })

  it('对话里点了某张修改卡：打开就选中它、直接看它', () => {
    const review = makeReview({ records: [summary({})], selectedId: 'cs-1', detailState: 'ready', detail: detail({ summary: '只改开头一段。' }) })
    renderPanel(review, { focusId: 'cs-1' })
    expect(review.select).toHaveBeenCalledWith('cs-1')
    expect(screen.getByRole('heading', { name: 'Agent 改了 001_山门将倾.md' })).toBeInTheDocument()
    expect(screen.getByText('只改开头一段。')).toBeInTheDocument()
    // 一行删一行加：画成对照
    expect(document.querySelector('.zm-diff-line.is-del')?.textContent).toContain('旧的一句。')
    expect(document.querySelector('.zm-diff-line.is-add')?.textContent).toContain('新的一句。')
  })

  it('写作检查的分数不混进改动说明', () => {
    const review = makeReview({
      records: [summary({})],
      selectedId: 'cs-1',
      detailState: 'ready',
      detail: detail({ summary: '只改开头一段，其余一字未动。；AI 味 52 分（medium）；对照这本书自己的写法……' }),
    })
    renderPanel(review, { focusId: 'cs-1' })
    expect(screen.getByText('只改开头一段，其余一字未动。')).toBeInTheDocument()
    expect(screen.queryByText(/medium/)).toBeNull()
  })

  it('没保存的：「保存」把接受和写入全过程交给持久数据层', () => {
    const pending = detail({ status: 'reviewing', review_revision: 4 }, [hunk({ hunk_id: 'h1' }), hunk({ hunk_id: 'h2', decision: 'accepted' })])
    const review = makeReview({ records: [summary({ status: 'reviewing' })], selectedId: 'cs-1', detailState: 'ready', detail: pending })
    renderPanel(review, { focusId: 'cs-1' })
    fireEvent.click(screen.getByRole('button', { name: C.save }))
    expect(review.apply).toHaveBeenCalledExactlyOnceWith(true)
    expect(review.decideMany).not.toHaveBeenCalled()
  })

  it('没保存的：「不要了」先问一句，点了才交给后台', async () => {
    const pending = detail({ status: 'reviewing', review_revision: 4 }, [hunk({})])
    const review = makeReview({ records: [summary({ status: 'reviewing' })], selectedId: 'cs-1', detailState: 'ready', detail: pending })
    const { askConfirm } = renderPanel(review, { focusId: 'cs-1' })
    fireEvent.click(screen.getByRole('button', { name: C.discard }))
    expect(review.discardByIds).not.toHaveBeenCalled()
    const request = vi.mocked(askConfirm).mock.calls[0][0] as ConfirmRequest
    expect(request.title).toBe(C.discardTitle)
    expect(request.danger).toBe(true)
    await request.onConfirm()
    expect(review.discardByIds).toHaveBeenCalledWith([{ changeSetId: 'cs-1', expectedReviewRevision: 4 }])
  })

  it('保存过的：能撤回，先问一句；撤回成了提示一句、刷新文件夹', async () => {
    const review = makeReview({ records: [summary({})], selectedId: 'cs-1', detailState: 'ready', detail: detail({}) })
    const view = renderPanel(review, { focusId: 'cs-1' })
    expect(screen.queryByRole('button', { name: C.save })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: C.undo }))
    const request = vi.mocked(view.askConfirm).mock.calls[0][0] as ConfirmRequest
    expect(request.title).toBe(C.undoTitle)
    await request.onConfirm()
    expect(review.rollback).toHaveBeenCalledTimes(1)

    view.rerender(makeReview({ ...review, detail: detail({ status: 'rolled_back' }) }))
    expect(view.notify).toHaveBeenCalledWith('ok', C.undone)
    expect(view.onRefreshProject).toHaveBeenCalledTimes(1)
  })

  it('挪了位置的文件：标出挪到哪', () => {
    const moved = detail({})
    moved.changes = [{ ...moved.changes[0], operation: 'move', target_relative_path: '05_正文内容/第 003 章 试剑.md', hunks: [] }]
    const review = makeReview({ records: [summary({})], selectedId: 'cs-1', detailState: 'ready', detail: moved })
    renderPanel(review, { focusId: 'cs-1' })
    expect(screen.getByText(C.fileMoved('05_正文内容/第 003 章 试剑.md'))).toBeInTheDocument()
  })

  it('详情里「全部修改」回到列表', () => {
    const review = makeReview({ records: [summary({})], selectedId: 'cs-1', detailState: 'ready', detail: detail({}) })
    renderPanel(review, { focusId: 'cs-1' })
    fireEvent.click(screen.getByRole('button', { name: C.back }))
    expect(document.querySelector('.zm-change-row')).not.toBeNull()
  })
})

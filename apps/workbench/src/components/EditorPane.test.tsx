import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { agentCandidateOutputFixture } from '../test/agentCandidateOutputFixture'
import { changeSetFixture } from '../test/changeSetFixture'
import { EditorPane } from './EditorPane'

const fileId = 'file-0123456789abcdef0123456789abcdef'
const relativePath = '05_正文内容/第一卷/CH001_正文.md'

describe('EditorPane live candidate views', () => {
  afterEach(cleanup)

  it('keeps the real manuscript read-only while exposing the persisted candidate and ChangeSet diff', async () => {
    const user = userEvent.setup()
    const candidate = await agentCandidateOutputFixture()
    const changeSet = changeSetFixture({ origin: 'agent' })
    const reviewChangeSet = vi.fn()

    render(
      <EditorPane
        content={'# 第一章\n\n雨下了一整夜。\n林见川在门口停了很久。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
        candidateOutput={candidate}
        changeSet={changeSet}
        onReviewChangeSet={reviewChangeSet}
      />,
    )

    expect(screen.getByRole('tab', { name: /助手结果/ })).toBeEnabled()
    expect(screen.getByRole('tab', { name: /修改对比/ })).toBeEnabled()
    expect(screen.getByRole('tab', { name: '审稿预览' })).toBeDisabled()

    await user.click(screen.getByRole('tab', { name: /助手结果/ }))
    expect(screen.getByRole('heading', { name: '写前修复方案' })).toBeInTheDocument()
    expect(screen.getByText('将周鹤亭的压力放入开场场景。')).toBeInTheDocument()
    expect(screen.getByText(/formal_draft_allowed=false/)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '采用' })).not.toBeInTheDocument()
    expect(screen.queryByText('压缩入场，提前显露威胁')).not.toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '查看修改对比' }))
    expect(screen.getByText((_, element) => (
      element?.tagName === 'PRE'
      && element.textContent === '雨下了一整夜。\n林见川在门口停了很久。'
    ))).toBeInTheDocument()
    expect(screen.getByText((_, element) => (
      element?.tagName === 'PRE'
      && element.textContent === '黄铜钥匙硌着掌心。\n林见川推开了门。'
    ))).toBeInTheDocument()
    expect(screen.getByText('待决定')).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '在右侧审阅' }))
    expect(reviewChangeSet).toHaveBeenCalledWith(changeSet.change_set_id)
  }, 15_000)

  it('does not unlock fixture candidate views for a real project without Runtime data', () => {
    render(
      <EditorPane
        content={'# 第一章\n\n真实正文。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
      />,
    )

    expect(screen.getByRole('tab', { name: '助手结果' })).toBeDisabled()
    expect(screen.getByRole('tab', { name: '修改对比' })).toBeDisabled()
    expect(screen.queryByText('压缩入场，提前显露威胁')).not.toBeInTheDocument()
  })

  it('waits for a requested Diff once and does not reopen it after apply refreshes the draft', async () => {
    const changeSet = changeSetFixture({ origin: 'agent' })
    const viewRequest = {
      mode: 'diff' as const,
      key: 7,
      changeSetId: changeSet.change_set_id,
    }
    const { rerender } = render(
      <EditorPane
        content={'# 第一章\n\n原始正文。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
        viewRequest={viewRequest}
      />,
    )

    expect(screen.getByRole('tab', { name: '修改对比' })).toBeDisabled()

    rerender(
      <EditorPane
        content={'# 第一章\n\n原始正文。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
        changeSet={changeSet}
        viewRequest={viewRequest}
      />,
    )
    expect(await screen.findByRole('tab', { name: /修改对比/ })).toHaveAttribute('aria-selected', 'true')

    rerender(
      <EditorPane
        content={'# 第一章\n\n写回后的正文。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
        changeSet={{ ...changeSet, status: 'applied' }}
        viewRequest={viewRequest}
      />,
    )
    expect(await screen.findByRole('tab', { name: '正文' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('写回后的正文。')).toBeInTheDocument()
  })

  it('exposes editable author prose with ChangeSet save controls when not read-only', () => {
    const onSave = vi.fn().mockResolvedValue(undefined)
    const { rerender } = render(
      <EditorPane
        content={'雨下了一整夜。'}
        fileId={fileId}
        relativePath={relativePath}
        onSave={onSave}
      />,
    )

    const editor = document.querySelector('.prose-editor')
    expect(editor).toBeTruthy()
    expect(editor).toHaveAttribute('contenteditable', 'true')
    expect(screen.getByText('已保存')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '保存正文' })).toBeDisabled()

    rerender(
      <EditorPane
        content={'雨下了一整夜。'}
        readOnly
        fileId={fileId}
        relativePath={relativePath}
        onSave={onSave}
      />,
    )
    expect(screen.getByText('只读快照')).toBeInTheDocument()
    expect(document.querySelector('.prose-editor')).toHaveAttribute('contenteditable', 'false')
    expect(screen.getByRole('button', { name: '保存正文' })).toBeDisabled()
  })
})

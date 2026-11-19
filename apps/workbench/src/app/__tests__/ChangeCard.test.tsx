import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ChangeCard } from '../chat/ChangeCard'
import type { ChangeSet } from '../../api/changeSets'

const api = vi.hoisted(() => ({ get: vi.fn() }))
vi.mock('../../api/changeSets', () => ({ getChangeSet: api.get }))
afterEach(cleanup)
beforeEach(() => {
  api.get.mockReset()
  api.get.mockResolvedValue({ changes: [{ relative_path: 'calc.py', operation: 'update', hunks: [{ hunk_id: 'h1', before_text: 'old', after_text: 'new' }] }] } as ChangeSet)
})

function show(status: 'applied' | 'pending' | 'rolled_back' = 'applied') {
  const actions = { onApprove: vi.fn(), onDiscard: vi.fn(), onRollback: vi.fn(), onShowInPanel: vi.fn(), onOpenFile: vi.fn() }
  render(<ChangeCard card={{ changeSetId: 'cs1', runId: 'r1', status, resultRevision: 2 }} snapshot={null} {...actions} />)
  return actions
}

describe('文件成果入口', () => {
  it('原地展开和收起真实差异，不打开右栏，文件名不重复堆叠', async () => {
    const actions = show()
    await screen.findByText('1 个文件改动')
    expect(screen.getAllByText('calc.py')).toHaveLength(1)
    expect(screen.getByText('已保存')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '查看改动' }))
    expect(actions.onShowInPanel).not.toHaveBeenCalled()
    expect(screen.getByRole('region', { name: 'calc.py 的改动' })).toBeInTheDocument()
    expect(screen.getByText('old')).toHaveClass('zm-diff-text')
    expect(screen.getByText('new')).toHaveClass('zm-diff-text')
    expect(screen.getAllByText('calc.py')).toHaveLength(1)
    expect(screen.getByRole('button', { name: '收起改动' })).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByRole('button', { name: '收起改动' }))
    expect(screen.queryByText('old')).toBeNull()
    expect(screen.getByRole('button', { name: '查看改动' })).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(screen.getByRole('button', { name: /calc.py/ }))
    expect(actions.onOpenFile).toHaveBeenCalledWith('calc.py')
    fireEvent.click(screen.getByRole('button', { name: '撤回' }))
    expect(actions.onRollback).toHaveBeenCalledOnce()
    expect(actions.onApprove).not.toHaveBeenCalled()
  })

  it('右栏入口独立可用，不必先展开内联差异', async () => {
    const actions = show()
    await screen.findByText('calc.py')
    fireEvent.click(screen.getByRole('button', { name: '在右栏查看' }))
    expect(actions.onShowInPanel).toHaveBeenCalledOnce()
    expect(screen.queryByText('old')).toBeNull()
    expect(screen.getByRole('button', { name: '查看改动' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('恢复说明和增删量，用字符单位说明实际逐行差异', async () => {
    api.get.mockResolvedValue({
      title: '修正求和结果', summary: '修正求和结果，保留打印语句。', origin: 'agent',
      changes: [{ relative_path: 'calc.py', operation: 'update', hunks: [{ hunk_id: 'h1', before_text: 'a = 1\nprint(a)', after_text: 'a = 10\nprint(a)' }] }],
    } as ChangeSet)
    show()
    await screen.findByText('修正求和结果，保留打印语句。')
    expect(screen.queryByText('修正求和结果')).toBeNull()
    expect(screen.getByText('删除 3 字符')).toBeInTheDocument()
    expect(screen.getByText('新增 4 字符')).toBeInTheDocument()
    expect(screen.getByTitle('按逐行差异统计增删字符，不计空白。')).toBeInTheDocument()
  })

  it('多个文件的差异跟着对应文件展示，删除的文件不提供失效的打开按钮', async () => {
    api.get.mockResolvedValue({ changes: [
      { relative_path: 'calc.py', operation: 'update', hunks: [{ hunk_id: 'h1', before_text: 'old', after_text: 'new' }] },
      { relative_path: 'removed.txt', operation: 'delete', hunks: [{ hunk_id: 'h1', before_text: 'removed', after_text: '' }] },
    ] } as ChangeSet)
    show()
    await screen.findByText('2 个文件改动')
    fireEvent.click(screen.getByRole('button', { name: '查看改动' }))
    expect(screen.getByRole('region', { name: 'calc.py 的改动' })).toHaveTextContent('old')
    expect(screen.getByRole('region', { name: 'removed.txt 的改动' })).toHaveTextContent('removed')
    expect(screen.queryByRole('button', { name: /removed.txt/ })).toBeNull()
  })

  it('待保存建议仍有保存与放弃，不误变成撤回', async () => {
    const actions = show('pending')
    await screen.findByText('calc.py')
    fireEvent.click(screen.getByRole('button', { name: '保存' }))
    fireEvent.click(screen.getByRole('button', { name: '放弃' }))
    expect(actions.onApprove).toHaveBeenCalledOnce()
    expect(actions.onDiscard).toHaveBeenCalledOnce()
    expect(screen.queryByRole('button', { name: '撤回' })).toBeNull()
  })

  it.each(['applied', 'reviewing', 'discarded', 'rolled_back'] as const)('移动记录%s的文件入口按实际应用状态定位', async (status) => {
    api.get.mockResolvedValue({ status, changes: [{ relative_path: 'old.py', target_relative_path: 'src/new.py', operation: 'move' }] } as ChangeSet)
    const actions = show(status === 'applied' ? 'applied' : status === 'rolled_back' ? 'rolled_back' : 'pending')
    fireEvent.click(await screen.findByRole('button', { name: /old.py.*src\/new.py/ }))
    expect(actions.onOpenFile).toHaveBeenCalledWith(status === 'applied' ? 'src/new.py' : 'old.py')
    fireEvent.click(screen.getByRole('button', { name: '查看改动' }))
    expect(screen.getByText('文件内容未改动')).toBeInTheDocument()
    expect(screen.queryByText(/新增 .*字符|删除 .*字符/)).toBeNull()
  })

  it('摘要读取失败可重试，仍可进入详情', async () => {
    api.get.mockRejectedValueOnce(new Error('offline'))
    const actions = show()
    const retry = await screen.findByRole('button', { name: '重新读取' })
    expect(screen.getByRole('button', { name: '查看改动' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '在右栏查看' }))
    expect(actions.onShowInPanel).toHaveBeenCalledOnce()
    fireEvent.click(retry)
    await screen.findByText('calc.py')
    await waitFor(() => expect(api.get).toHaveBeenCalledTimes(2))
    fireEvent.click(screen.getByRole('button', { name: '查看改动' }))
    expect(screen.getByText('new')).toBeInTheDocument()
  })
})

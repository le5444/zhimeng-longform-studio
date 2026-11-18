import { act, cleanup, fireEvent, render, renderHook, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { getSessionFileChange, listSessionFileChanges, undoSessionFileChange, type SessionFileChangeDetail } from '../../api/fileChanges'
import { useSessionFileChanges } from '../../hooks/useSessionFileChanges'
import { SessionChangesPanel as SessionChangesView } from '../panel/SessionChangesPanel'

vi.mock('../../api/fileChanges', () => ({ getSessionFileChange: vi.fn(), listSessionFileChanges: vi.fn(), undoSessionFileChange: vi.fn() }))

function SessionChangesPanel({ sessionId, onChanged, visible = true, ...props }: {
  sessionId: string | null; onChanged?: () => void; visible?: boolean; focusId?: string
}) {
  const review = useSessionFileChanges(sessionId, '', onChanged, visible)
  return visible ? <SessionChangesView sessionId={sessionId} review={review} {...props} /> : null
}

function detail(sessionId = 's1', id = 'filechange-1', undone = false): SessionFileChangeDetail {
  const file = { sequence: 0, operation: 'update' as const, path: 'C:/scratch/notes.txt', target_path: null, undone_at: undone ? '2026-10-09T01:01:00Z' : null }
  return {
    session_id: sessionId, change_id: id, run_id: 'r1', title: `整理笔记 ${sessionId}`,
    created_at: '2026-10-09T01:00:00Z', undone_at: file.undone_at, status: undone ? 'undone' : 'applied', files: [file],
    previews: [{ ...file, before_text: undone ? null : '修改前', after_text: undone ? null : '修改后', masked_secrets: 0, preview_note: undone ? '已撤回，仅显示操作记录。' : null }],
    undo_available: !undone, undo_reason: undone ? '这次修改已全部撤回。' : null,
  }
}

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (error: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(listSessionFileChanges).mockImplementation(async (sid) => [detail(sid)])
  vi.mocked(getSessionFileChange).mockImplementation(async (sid, id) => detail(sid, id))
  vi.mocked(undoSessionFileChange).mockImplementation(async (sid, id) => detail(sid, id, true))
})
afterEach(cleanup)

describe('普通对话文件修改', () => {
  it.each([
    ['failed', '写入失败，尚未还原'], ['unverified', '保存结果待核对'],
  ] as const)('列表与详情保留 %s，撤回后显示真实终态', async (status, label) => {
    const value = { ...detail(), status }
    vi.mocked(listSessionFileChanges).mockResolvedValue([value])
    vi.mocked(getSessionFileChange).mockResolvedValue(value)
    render(<SessionChangesPanel sessionId="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: new RegExp(label) }))
    expect(await screen.findByText(label)).toBeInTheDocument()
    expect(screen.queryByText('已保存')).not.toBeInTheDocument()
    vi.mocked(listSessionFileChanges).mockResolvedValue([detail('s1', 'filechange-1', true)])
    vi.mocked(getSessionFileChange).mockResolvedValue(detail('s1', 'filechange-1', true))
    fireEvent.click(screen.getByRole('button', { name: '撤回这次修改' }))
    expect(await screen.findByText('这次修改已全部撤回。')).toBeInTheDocument()
    expect(screen.queryByText(label)).not.toBeInTheDocument()
  })

  it('从持久列表查看真实对照，直接撤回并保留已撤回记录', async () => {
    const changed = vi.fn()
    render(<SessionChangesPanel sessionId="s1" onChanged={changed} />)
    fireEvent.click(await screen.findByRole('button', { name: /整理笔记 s1/ }))
    expect(await screen.findByText('修改前')).toBeInTheDocument()
    expect(screen.getByText('修改后')).toBeInTheDocument()
    vi.mocked(getSessionFileChange).mockResolvedValue(detail('s1', 'filechange-1', true))
    vi.mocked(listSessionFileChanges).mockResolvedValue([detail('s1', 'filechange-1', true)])
    fireEvent.click(screen.getByRole('button', { name: '撤回这次修改' }))
    await waitFor(() => expect(undoSessionFileChange).toHaveBeenCalledWith('s1', 'filechange-1'))
    expect(await screen.findByText('这次修改已全部撤回。')).toBeInTheDocument()
    expect(changed).toHaveBeenCalledTimes(1)
    expect(screen.queryByRole('button', { name: '撤回这次修改' })).not.toBeInTheDocument()
    expect(screen.queryByText('修改后')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '返回记录' }))
    expect(await screen.findByRole('button', { name: /已撤回/ })).toBeInTheDocument()
  })

  it('文件冲突时显示原因、不编造对照、不让按钮声称撤回成功', async () => {
    const conflict = detail()
    conflict.undo_available = false
    conflict.undo_reason = '文件后来又被改过，已保留现有内容。'
    conflict.previews[0] = { ...conflict.previews[0], before_text: null, after_text: null, preview_note: '无法核验历史对照。' }
    vi.mocked(getSessionFileChange).mockResolvedValue(conflict)
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    expect(await screen.findByText(conflict.undo_reason)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤回这次修改' })).toBeDisabled()
    expect(document.querySelector('.zm-diff-line')).toBeNull()
    expect(undoSessionFileChange).not.toHaveBeenCalled()
    expect(screen.queryByText(/修改前 .*字符/)).toBeNull()
  })

  it('突出当前核对原因，重新核对后按服务器事实恢复对照和撤回', async () => {
    const conflict = detail()
    conflict.undo_available = false
    conflict.undo_reason = '文件后来又被改过，已保留现有内容。'
    conflict.previews[0] = { ...conflict.previews[0], before_text: null, after_text: null,
      preview_note: '文件内容已变化，无法核验完整对照。' }
    vi.mocked(getSessionFileChange).mockResolvedValueOnce(conflict).mockResolvedValue(detail())
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    expect(await screen.findByRole('status', { name: '当前文件核对' })).toHaveTextContent('当前暂不能撤回')
    expect(screen.getByRole('button', { name: '撤回这次修改' })).toBeDisabled()
    expect(screen.queryByText('修改后')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '重新核对' }))
    expect(await screen.findByText('修改后')).toBeInTheDocument()
    expect(screen.queryByRole('status', { name: '当前文件核对' })).toBeNull()
    expect(screen.getByRole('button', { name: '撤回这次修改' })).toBeEnabled()
    expect(undoSessionFileChange).not.toHaveBeenCalled()
  })

  it.each(['failed', 'unverified'] as const)('保留%s事实，并提供原地核对入口', async (status) => {
    const value = { ...detail(), status }
    vi.mocked(getSessionFileChange).mockResolvedValue(value)
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    expect(await screen.findByRole('status', { name: '当前文件核对' })).toHaveTextContent('现有文件与可用备份已保留')
    expect(screen.getByRole('button', { name: '重新核对' })).toBeEnabled()
    expect(screen.queryByText('已保存')).toBeNull()
  })

  it('显示后端核验的字符总数，遮密钥后的预览长度不冒充原始长度', async () => {
    const value = detail()
    value.previews[0] = { ...value.previews[0], before_text: 'key=***', after_text: '',
      before_characters: 1234, after_characters: 0, masked_secrets: 1 }
    vi.mocked(getSessionFileChange).mockResolvedValue(value)
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    expect(await screen.findByText('修改前 1,234 字符 → 修改后 0 字符')).toBeInTheDocument()
    expect(screen.getByTitle('按核验后的文本计数，包含空白，每次换行计 1 字符，不含 BOM。')).toBeInTheDocument()
    expect(undoSessionFileChange).not.toHaveBeenCalled()
  })

  it('重新核对期间保留记录但暂停旧对照与撤回，冲突响应到达后显示当前原因', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    vi.mocked(getSessionFileChange).mockResolvedValueOnce(detail()).mockReturnValueOnce(pending.promise)
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    expect(await screen.findByText('修改后')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '刷新' }))
    expect(screen.getByText('正在核对文件状态…')).toBeInTheDocument()
    expect(screen.getByText('整理笔记 s1')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: '撤回这次修改' })).toBeDisabled()
    expect(screen.queryByText('修改后')).toBeNull()
    const conflict = { ...detail(), undo_available: false, undo_reason: '外部编辑已保留。',
      previews: [{ ...detail().previews[0], before_text: null, after_text: null }] }
    await act(async () => pending.resolve(conflict))
    expect(screen.getByRole('status', { name: '当前文件核对' })).toHaveTextContent('外部编辑已保留。')
    expect(screen.queryByText('正在核对文件状态…')).toBeNull()
    expect(screen.getByRole('button', { name: '撤回这次修改' })).toBeDisabled()
    expect(undoSessionFileChange).not.toHaveBeenCalled()
  })

  it('已撤回或旧接口没有字符总数时不推算历史大小', async () => {
    vi.mocked(getSessionFileChange).mockResolvedValue(detail('s1', 'filechange-1', true))
    render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    await screen.findByText('这次修改已全部撤回。')
    expect(screen.queryByText(/修改前 .*字符/)).toBeNull()
  })

  it('列表加载失败可就地重试', async () => {
    vi.mocked(listSessionFileChanges).mockRejectedValueOnce(new Error('暂时离线'))
    render(<SessionChangesPanel sessionId="s1" />)
    fireEvent.click(await screen.findByRole('button', { name: '重试' }))
    expect(await screen.findByRole('button', { name: /整理笔记 s1/ })).toBeInTheDocument()
  })

  it('详情加载失败可重试，切会话即清掉旧内容', async () => {
    vi.mocked(getSessionFileChange).mockRejectedValueOnce(new Error('读取失败'))
    const view = render(<SessionChangesPanel sessionId="s1" focusId="filechange-1" />)
    fireEvent.click(await screen.findByRole('button', { name: '重试' }))
    expect(await screen.findByText('修改后')).toBeInTheDocument()
    view.rerender(<SessionChangesPanel sessionId="s2" />)
    expect(screen.queryByText('修改后')).not.toBeInTheDocument()
    expect(await screen.findByRole('button', { name: /整理笔记 s2/ })).toBeInTheDocument()
  })
})

describe('文件修改请求隔离', () => {
  it('关闭右栏时暂停查询，已发出的撤回继续完成，重开显示实际结果', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    const changed = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    const view = render(<SessionChangesPanel sessionId="s1" onChanged={changed} focusId="filechange-1" />)
    await screen.findByText('修改后')
    fireEvent.click(screen.getByRole('button', { name: '撤回这次修改' }))
    view.rerender(<SessionChangesPanel sessionId="s1" onChanged={changed} visible={false} />)
    const listCalls = vi.mocked(listSessionFileChanges).mock.calls.length
    const detailCalls = vi.mocked(getSessionFileChange).mock.calls.length
    vi.mocked(getSessionFileChange).mockResolvedValue(detail('s1', 'filechange-1', true))
    vi.mocked(listSessionFileChanges).mockResolvedValue([detail('s1', 'filechange-1', true)])
    await act(async () => pending.resolve(detail('s1', 'filechange-1', true)))
    expect(changed).toHaveBeenCalledTimes(1)
    expect(listSessionFileChanges).toHaveBeenCalledTimes(listCalls)
    expect(getSessionFileChange).toHaveBeenCalledTimes(detailCalls)
    view.rerender(<SessionChangesPanel sessionId="s1" onChanged={changed} focusId="filechange-1" />)
    expect(await screen.findByText('这次修改已全部撤回。')).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '撤回这次修改' })).not.toBeInTheDocument()
  })

  it('主界面卸载后不调用已经失效的文件刷新', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    const changed = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    const { result, unmount } = renderHook(() => useSessionFileChanges('s1', '', changed))
    act(() => { void result.current.undo('filechange-1') })
    unmount()
    await act(async () => pending.resolve(detail('s1', 'filechange-1', true)))
    expect(changed).not.toHaveBeenCalled()
  })

  it('切会话后迟到的旧列表不能盖掉新列表，切回来也重新读', async () => {
    const first = deferred<SessionFileChangeDetail[]>()
    vi.mocked(listSessionFileChanges).mockImplementationOnce(() => first.promise)
    const { result, rerender } = renderHook(({ sid }) => useSessionFileChanges(sid), { initialProps: { sid: 's1' } })
    rerender({ sid: 's2' })
    await waitFor(() => expect(result.current.records[0]?.session_id).toBe('s2'))
    await act(async () => first.resolve([detail('s1')]))
    expect(result.current.records[0].session_id).toBe('s2')
    rerender({ sid: 's1' })
    expect(result.current.records).toEqual([])
    await waitFor(() => expect(result.current.records[0]?.session_id).toBe('s1'))
  })

  it('快速切换修改记录时迟到的旧详情不能串入新详情', async () => {
    const old = deferred<SessionFileChangeDetail>()
    vi.mocked(getSessionFileChange).mockImplementationOnce(() => old.promise)
    const { result } = renderHook(() => useSessionFileChanges('s1'))
    act(() => result.current.select('filechange-old'))
    act(() => result.current.select('filechange-new'))
    await waitFor(() => expect(result.current.detail?.change_id).toBe('filechange-new'))
    await act(async () => old.resolve(detail('s1', 'filechange-old')))
    expect(result.current.detail?.change_id).toBe('filechange-new')
  })

  it('撤回处理中切会话，旧结果不污染当前对话', async () => {
    const undone = deferred<SessionFileChangeDetail>()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(undone.promise)
    const { result, rerender } = renderHook(({ sid }) => useSessionFileChanges(sid), { initialProps: { sid: 's1' } })
    act(() => result.current.select('filechange-1'))
    await waitFor(() => expect(result.current.detail).not.toBeNull())
    act(() => { void result.current.undo('filechange-1') })
    rerender({ sid: 's2' })
    await act(async () => undone.resolve(detail('s1', 'filechange-1', true)))
    expect(result.current.detail).toBeNull()
    expect(result.current.actionError).toBeNull()
    await waitFor(() => expect(result.current.records[0]?.session_id).toBe('s2'))
  })

  it('撤回时切走再回来，完成后重读当前列表和详情并通知当前文件视图', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    const changed = vi.fn()
    const currentChanged = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    const { result, rerender } = renderHook(({ sid, onChanged }) => useSessionFileChanges(sid, '', onChanged), {
      initialProps: { sid: 's1', onChanged: changed },
    })
    await waitFor(() => expect(result.current.records).toHaveLength(1))
    act(() => { void result.current.undo('filechange-1') })
    rerender({ sid: 's2', onChanged: changed })
    rerender({ sid: 's1', onChanged: currentChanged })
    act(() => result.current.select('filechange-1'))
    await waitFor(() => expect(result.current.detail?.status).toBe('applied'))
    expect(result.current.busy).toBe(true)
    vi.mocked(getSessionFileChange).mockResolvedValue(detail('s1', 'filechange-1', true))
    vi.mocked(listSessionFileChanges).mockResolvedValue([detail('s1', 'filechange-1', true)])
    await act(async () => pending.resolve(detail('s1', 'filechange-1', true)))
    await waitFor(() => expect(result.current.records[0]?.status).toBe('undone'))
    await waitFor(() => expect(result.current.detail?.status).toBe('undone'))
    expect(result.current.busy).toBe(false)
    expect(currentChanged).toHaveBeenCalledTimes(1)
    expect(changed).not.toHaveBeenCalled()
  })

  it('另一个对话可以撤回自己的修改，各自完成时更新各自状态', async () => {
    const first = deferred<SessionFileChangeDetail>()
    const second = deferred<SessionFileChangeDetail>()
    const changed = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise)
    const { result, rerender } = renderHook(({ sid }) => useSessionFileChanges(sid, '', changed), { initialProps: { sid: 's1' } })
    act(() => { void result.current.undo('filechange-1') })
    rerender({ sid: 's2' })
    expect(result.current.busy).toBe(false)
    act(() => { void result.current.undo('filechange-1') })
    expect(undoSessionFileChange).toHaveBeenCalledTimes(2)
    expect(result.current.busy).toBe(true)
    await act(async () => first.resolve(detail('s1', 'filechange-1', true)))
    expect(result.current.busy).toBe(true)
    expect(changed).toHaveBeenCalledTimes(1)
    await act(async () => second.resolve(detail('s2', 'filechange-1', true)))
    expect(result.current.busy).toBe(false)
    expect(changed).toHaveBeenCalledTimes(2)
  })

  it('后台对话的撤回完成时也通知当前文件视图，保留当前对话的记录', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    const oldChanged = vi.fn()
    const currentChanged = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    const { result, rerender } = renderHook(({ sid, onChanged }) => useSessionFileChanges(sid, '', onChanged), {
      initialProps: { sid: 's1', onChanged: oldChanged },
    })
    act(() => { void result.current.undo('filechange-1') })
    rerender({ sid: 's2', onChanged: currentChanged })
    await waitFor(() => expect(result.current.records[0]?.session_id).toBe('s2'))
    await act(async () => pending.resolve(detail('s1', 'filechange-1', true)))
    expect(currentChanged).toHaveBeenCalledOnce()
    expect(oldChanged).not.toHaveBeenCalled()
    expect(result.current.records[0]?.session_id).toBe('s2')
    expect(result.current.detail).toBeNull()
  })

  it('切走再回来后的部分撤回失败仍重读事实、刷新文件并保留错误', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    const changed = vi.fn()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    const { result, rerender } = renderHook(({ sid }) => useSessionFileChanges(sid, '', changed), { initialProps: { sid: 's1' } })
    act(() => { void result.current.undo('filechange-1') })
    rerender({ sid: 's2' })
    rerender({ sid: 's1' })
    act(() => result.current.select('filechange-1'))
    await waitFor(() => expect(result.current.detail?.status).toBe('applied'))
    const partial = { ...detail(), status: 'partially_undone' as const }
    vi.mocked(getSessionFileChange).mockResolvedValue(partial)
    vi.mocked(listSessionFileChanges).mockResolvedValue([partial])
    await act(async () => pending.reject(new Error('已有 1/2 个文件撤回，其余被占用。')))
    await waitFor(() => expect(result.current.detail?.status).toBe('partially_undone'))
    expect(result.current.records[0]?.status).toBe('partially_undone')
    expect(result.current.actionError).toContain('1/2')
    expect(result.current.busy).toBe(false)
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('部分撤回报错后刷新真实状态，允许继续处理剩余项', async () => {
    const partial = { ...detail(), status: 'partially_undone' as const }
    const changed = vi.fn()
    const { result } = renderHook(() => useSessionFileChanges('s1', '', changed))
    act(() => result.current.select('filechange-1'))
    await waitFor(() => expect(result.current.detail).not.toBeNull())
    vi.mocked(undoSessionFileChange).mockRejectedValueOnce(new Error('已有 1/2 个文件撤回，其余被占用。'))
    vi.mocked(getSessionFileChange).mockResolvedValue(partial)
    await act(async () => { await result.current.undo('filechange-1') })
    await waitFor(() => expect(result.current.detail?.status).toBe('partially_undone'))
    expect(result.current.actionError).toContain('1/2')
    expect(result.current.detail?.undo_available).toBe(true)
    expect(changed).toHaveBeenCalledTimes(1)
  })

  it('同一时刻只提交一次撤回，详情会话不匹配则拒绝显示', async () => {
    const pending = deferred<SessionFileChangeDetail>()
    vi.mocked(undoSessionFileChange).mockReturnValueOnce(pending.promise)
    vi.mocked(getSessionFileChange).mockResolvedValueOnce(detail('wrong-session'))
    const { result } = renderHook(() => useSessionFileChanges('s1'))
    act(() => result.current.select('filechange-1'))
    await waitFor(() => expect(result.current.detailError).toContain('不一致'))
    expect(result.current.detail).toBeNull()
    act(() => { void result.current.undo('filechange-1'); void result.current.undo('filechange-1') })
    expect(undoSessionFileChange).toHaveBeenCalledTimes(1)
    await act(async () => pending.resolve(detail('s1', 'filechange-1', true)))
  })
})

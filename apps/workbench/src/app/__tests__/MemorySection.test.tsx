/**
 * 设置 → 记忆（9-29）：开关、一条一条改删、自己加一条、整份直接改；存的时候带指纹，Agent 刚记过（409）就重读。
 * 接口全部 mock，不启动服务。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RuntimeApiError } from '../../api/runtime'
import type { UserMemoryView } from '../../api/userMemory'
import { MemoryTab, appendMemoryLine, memoryEntries, replaceMemoryLine } from '../settings/MemorySection'
import { ST } from '../settings/settings.text'
import { S } from '../settings/settingsText'

const api = vi.hoisted(() => ({
  readUserMemory: vi.fn(),
  saveUserMemory: vi.fn(),
  saveMemoryEnabled: vi.fn(),
  readMemoryHistory: vi.fn(),
  readMemoryVersion: vi.fn(),
}))
vi.mock('../../api/userMemory', () => api)

afterEach(cleanup)

function view(over: Partial<UserMemoryView> = {}): UserMemoryView {
  return {
    schema: 'zhimeng.user-memory.v1',
    path: 'C:/repo/.runtime/用户记忆.md',
    exists: true,
    content: '我不喜欢每句另起一行\n- 回答别太长\n',
    sha256: 'a'.repeat(64),
    enabled: true,
    project_memory_file: 'AGENTS.md',
    ...over,
  }
}

describe('记忆的行', () => {
  it('一行一条：空行不算，行首的「- 」只是记号', () => {
    expect(memoryEntries('甲\n\n- 乙\r\n  * 丙\n')).toEqual([
      { line: 0, text: '甲', prefix: '' },
      { line: 2, text: '乙', prefix: '- ' },
      { line: 3, text: '丙', prefix: '  * ' },
    ])
  })

  it('改一条留着记号和换行符，删一条别的不动；加一条接在最后', () => {
    const content = '甲\n- 乙\r\n丙\n'
    const [first, second] = memoryEntries(content)
    expect(replaceMemoryLine(content, second, '乙改')).toBe('甲\n- 乙改\r\n丙\n')
    expect(replaceMemoryLine(content, first, null)).toBe('- 乙\r\n丙\n')
    expect(appendMemoryLine(content, '丁')).toBe('甲\n- 乙\r\n丙\n丁\n')
    expect(appendMemoryLine('  \n', '丁')).toBe('丁\n')
  })
})

describe('设置 → 记忆', () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset()
    api.readUserMemory.mockResolvedValue(view())
  })

  it('开关：开着时写着 Agent 会自己记，点一下关掉并提示', async () => {
    const user = userEvent.setup()
    const notify = vi.fn()
    api.saveMemoryEnabled.mockResolvedValue(view({ enabled: false }))
    render(<MemoryTab notify={notify} />)

    const toggle = await screen.findByRole('switch', { name: ST.memorySwitch })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    expect(screen.getByText(ST.memoryIntroOn)).toBeInTheDocument()
    await user.click(toggle)
    await waitFor(() => expect(api.saveMemoryEnabled).toHaveBeenCalledWith(false))
    expect(notify).toHaveBeenCalledWith('ok', ST.memoryTurnedOff)
    expect(screen.getByRole('switch', { name: ST.memorySwitch })).toHaveAttribute('aria-checked', 'false')
    expect(screen.getByText(ST.memoryIntroOff)).toBeInTheDocument()
  })

  it('一条一条列出来：删一条、改一条、加一条，每次都带着读到时的指纹存整份', async () => {
    const user = userEvent.setup()
    const notify = vi.fn()
    api.saveUserMemory.mockImplementation(async (content: string) => ({ ...view({ content, sha256: 'b'.repeat(64) }), ok: true }))
    render(<MemoryTab notify={notify} />)

    const list = await screen.findByRole('list', { name: ST.memoryListLabel })
    expect(within(list).getAllByRole('listitem').map((item) => item.querySelector('.zm-memory-text')?.textContent)).toEqual([
      '我不喜欢每句另起一行',
      '回答别太长',
    ])

    await user.click(screen.getByRole('button', { name: ST.memoryDeleteOne('我不喜欢每句另起一行') }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenCalledWith('- 回答别太长\n', 'a'.repeat(64)))
    expect(notify).toHaveBeenCalledWith('ok', ST.memoryDeleted)

    await user.click(screen.getByRole('button', { name: ST.memoryEditOne('回答别太长') }))
    const box = screen.getByRole('textbox', { name: ST.memoryEditLabel })
    await user.clear(box)
    await user.type(box, '回答三五句就够')
    await user.click(screen.getByRole('button', { name: ST.memorySave }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenLastCalledWith('- 回答三五句就够\n', 'b'.repeat(64)))

    await user.type(screen.getByRole('textbox', { name: ST.memoryAddPlaceholder }), '我写仙侠')
    await user.click(screen.getByRole('button', { name: ST.memoryAdd }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenLastCalledWith('- 回答三五句就够\n我写仙侠\n', 'b'.repeat(64)))
    expect(notify).toHaveBeenCalledWith('ok', ST.memoryAdded)
    expect(screen.getByRole('textbox', { name: ST.memoryAddPlaceholder })).toHaveValue('')
  })

  it('开关损坏时显示暂停原因，明确重新开启后清除提示且保留记忆', async () => {
    const user = userEvent.setup()
    const settingsError = '记忆开关暂时无法读取，Agent 已暂停使用和自动保存用户记忆。'
    api.readUserMemory.mockResolvedValue(view({ enabled: false, settings_error: settingsError }))
    api.saveMemoryEnabled.mockResolvedValue(view({ settings_error: null }))
    render(<MemoryTab notify={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent(settingsError)
    expect(screen.queryByText(ST.memoryIntroOff)).not.toBeInTheDocument()
    await user.click(screen.getByRole('switch', { name: ST.memorySwitch }))
    await waitFor(() => expect(api.saveMemoryEnabled).toHaveBeenCalledWith(true))
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByText('回答别太长')).toBeInTheDocument()
  })

  it('存的时候 Agent 刚记过一条（409）：提示、重读，新记的那条在列表里', async () => {
    const user = userEvent.setup()
    const notify = vi.fn()
    api.saveUserMemory.mockRejectedValue(new RuntimeApiError(409, { error: { code: 'user_memory_changed', message: '记忆在你改的时候又多了内容' } }))
    render(<MemoryTab notify={notify} />)
    await screen.findByRole('list', { name: ST.memoryListLabel })

    api.readUserMemory.mockResolvedValue(view({ content: '我不喜欢每句另起一行\n- 回答别太长\n笔名叫青崖\n', sha256: 'c'.repeat(64) }))
    await user.click(screen.getByRole('button', { name: ST.memoryDeleteOne('回答别太长') }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('warn', expect.stringContaining('又多了内容')))
    expect(await screen.findByText('笔名叫青崖')).toBeInTheDocument()
    expect(screen.getByText('回答别太长')).toBeInTheDocument()
  })

  it('没记过东西时说怎么来；读不出来时提示、给「再读一次」', async () => {
    const notify = vi.fn()
    api.readUserMemory.mockResolvedValueOnce(view({ content: '' }))
    const { unmount } = render(<MemoryTab notify={notify} />)
    expect(await screen.findByText(ST.memoryEmpty)).toBeInTheDocument()
    unmount()

    api.readUserMemory.mockRejectedValueOnce(new Error('HTTP 500'))
    render(<MemoryTab notify={notify} />)
    await waitFor(() => expect(notify).toHaveBeenCalledWith('danger', expect.stringContaining('HTTP 500')))
    expect(screen.getByRole('button', { name: ST.memoryRetry })).toBeInTheDocument()
    expect(screen.queryByRole('switch')).toBeNull()
  })

  it('搜索、查看历史并恢复；恢复仍带当前指纹', async () => {
    const user = userEvent.setup()
    api.readMemoryHistory.mockResolvedValue({ items: [{ version: 'v1', saved_at: '2026-10-07T10:00:00Z', characters: 5 }], next_offset: null })
    api.readMemoryVersion.mockResolvedValue({ version: 'v1', content: '之前的偏好', saved_at: '2026-10-07T10:00:00Z' })
    api.saveUserMemory.mockImplementation(async (content: string) => view({ content, sha256: 'b'.repeat(64) }))
    render(<MemoryTab notify={vi.fn()} />)
    await screen.findByRole('list', { name: ST.memoryListLabel })
    await user.type(screen.getByRole('textbox', { name: S.memory.search }), '回答')
    expect(screen.queryByText('我不喜欢每句另起一行')).toBeNull()
    expect(screen.getByText('回答别太长')).toBeInTheDocument()
    await user.click(screen.getByRole('tab', { name: '修改历史' }))
    await user.click(screen.getByRole('button', { name: S.memory.loadHistory }))
    await user.click(await screen.findByRole('button', { name: S.memory.preview }))
    expect(await screen.findByText('之前的偏好')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: S.memory.restore }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenCalledWith('之前的偏好', 'a'.repeat(64)))
  })

  it('整份编辑时刷新取得后台新记忆，旧草稿不能拿新指纹覆盖', async () => {
    const user = userEvent.setup()
    render(<MemoryTab notify={vi.fn()} />)
    await screen.findByRole('list', { name: ST.memoryListLabel })
    await user.click(screen.getByRole('tab', { name: '全文编辑' }))
    const editor = await screen.findByRole('textbox', { name: ST.memoryRawLabel })
    await user.clear(editor)
    await user.type(editor, '未保存草稿')
    api.readUserMemory.mockResolvedValue(view({ content: '后台刚记的内容', sha256: 'b'.repeat(64) }))
    await user.click(screen.getByRole('button', { name: S.memory.refresh }))
    expect(await screen.findByRole('alert')).toHaveTextContent(S.memory.stale)
    expect(editor).toHaveValue('未保存草稿')
    expect(screen.getByRole('button', { name: ST.memorySave })).toBeDisabled()
    expect(api.saveUserMemory).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: S.memory.reload }))
    expect(editor).toHaveValue('后台刚记的内容')
  })

  it('全文草稿跨分页保留，保存失败也能继续重试', async () => {
    const user = userEvent.setup()
    const notify = vi.fn()
    api.saveUserMemory.mockRejectedValueOnce(new Error('offline')).mockImplementationOnce(async content => view({ content, sha256: 'c'.repeat(64) }))
    render(<MemoryTab notify={notify} />)
    await user.click(await screen.findByRole('tab', { name: '全文编辑' }))
    const editor = await screen.findByRole('textbox', { name: ST.memoryRawLabel })
    await user.clear(editor)
    await user.type(editor, '只保存这份经过检查的偏好')
    await user.click(screen.getByRole('tab', { name: /^逐行编辑/ }))
    expect(editor).not.toBeVisible()
    expect(screen.getByRole('button', { name: ST.memoryDeleteOne('回答别太长') })).toBeDisabled()
    await user.click(screen.getByRole('tab', { name: '全文编辑' }))
    expect(editor).toHaveValue('只保存这份经过检查的偏好')
    await user.click(screen.getByRole('button', { name: ST.memorySave }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('danger', expect.stringContaining('offline')))
    expect(editor).toHaveValue('只保存这份经过检查的偏好')
    await user.click(screen.getByRole('button', { name: ST.memorySave }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenCalledTimes(2))
    expect(api.saveUserMemory).toHaveBeenLastCalledWith('只保存这份经过检查的偏好', 'a'.repeat(64))
  })
  it('迟到的刷新结果不能覆盖刚保存的记忆', async () => {
    let finish!: (value: UserMemoryView) => void
    api.saveUserMemory.mockResolvedValue(view({ content: '已保存的新内容', sha256: 'd'.repeat(64) }))
    render(<MemoryTab notify={vi.fn()} />)
    await screen.findByRole('list', { name: ST.memoryListLabel })
    api.readUserMemory.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
    fireEvent.click(screen.getByRole('button', { name: S.memory.refresh }))
    fireEvent.click(screen.getByRole('tab', { name: '全文编辑' }))
    const editor = await screen.findByRole('textbox', { name: ST.memoryRawLabel })
    fireEvent.change(editor, { target: { value: '已保存的新内容' } })
    fireEvent.click(screen.getByRole('button', { name: ST.memorySave }))
    await waitFor(() => expect(api.saveUserMemory).toHaveBeenCalled())
    await act(async () => { finish(view()) })
    expect(editor).toHaveValue('已保存的新内容')
    expect(screen.queryByText(S.memory.stale)).toBeNull()
    expect(screen.getByRole('button', { name: ST.memorySave })).toBeDisabled()
  })

})

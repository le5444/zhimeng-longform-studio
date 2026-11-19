import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import CommandRecovery from '../panel/CommandRecovery'
import type { CommandRecovery as Recovery } from '../../api/commandSessions'

const api = vi.hoisted(() => ({ inspectCommandRecovery: vi.fn(), restoreCommandFile: vi.fn() }))
vi.mock('../../api/commandSessions', () => api)

const fixture = (): Recovery => ({ observation_id: 'observation', source: 'current_check', problems: [], restorations: [], previewed_path: 'C:/work/report.txt',
  entries: [{ path: 'C:/work/report.txt', revision: 'a'.repeat(64), status: 'available', action: 'restore',
    before_sha256: 'before', current_sha256: 'current', before_text: '执行前报告', current_text: '命令写入的报告', note: null }] })

beforeEach(() => { vi.resetAllMocks(); api.inspectCommandRecovery.mockResolvedValue(fixture()) })
afterEach(cleanup)

async function check() {
  fireEvent.click(screen.getByRole('button', { name: '核对文件与备份' }))
  await screen.findByRole('button', { name: '恢复此文件到执行前版本' })
}

describe('command recovery', () => {
  it('checks before offering a versioned restore and refreshes actual files', async () => {
    const changed = vi.fn()
    api.restoreCommandFile.mockResolvedValue({ request_id: 'saved', path: fixture().entries[0].path, change_id: 'change', status: 'applied' })
    render(<CommandRecovery sessionId="chat" commandId="cmd" onChanged={changed} />)
    expect(api.inspectCommandRecovery).not.toHaveBeenCalled()
    await check()
    expect(screen.getByText(/变化来源尚未确认/)).toBeVisible()
    expect(api.restoreCommandFile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '恢复此文件到执行前版本' }))
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(api.restoreCommandFile).toHaveBeenCalledWith('chat', 'cmd', expect.objectContaining({ path: 'C:/work/report.txt', revision: 'a'.repeat(64), request_id: expect.any(String) }))
    expect(screen.getByRole('status')).toHaveTextContent('恢复已保存')
  })

  it('reads back a lost reply and does not send another restore', async () => {
    const changed = vi.fn()
    api.restoreCommandFile.mockImplementation(async (_sid, _cid, request) => {
      api.inspectCommandRecovery.mockResolvedValue({ ...fixture(), restorations: [{ ...request, status: 'applied', change_id: 'saved' }] })
      throw new Error('连接中断')
    })
    render(<CommandRecovery sessionId="chat" commandId="cmd" onChanged={changed} />)
    await check()
    fireEvent.click(screen.getByRole('button', { name: '恢复此文件到执行前版本' }))
    await waitFor(() => expect(changed).toHaveBeenCalledOnce())
    expect(api.restoreCommandFile).toHaveBeenCalledOnce()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('恢复已保存')
  })

  it('keeps the request identity when a failed operation is explicitly retried', async () => {
    api.restoreCommandFile.mockRejectedValue(new Error('连接中断'))
    render(<CommandRecovery sessionId="chat" commandId="cmd" />)
    await check()
    fireEvent.click(screen.getByRole('button', { name: '恢复此文件到执行前版本' }))
    await screen.findByRole('alert')
    await waitFor(() => expect(screen.getByRole('button', { name: '重新核对文件与备份' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '重新核对文件与备份' }))
    await waitFor(() => expect(screen.getByRole('button', { name: '恢复此文件到执行前版本' })).toBeEnabled())
    fireEvent.click(screen.getByRole('button', { name: '恢复此文件到执行前版本' }))
    await waitFor(() => expect(api.restoreCommandFile).toHaveBeenCalledTimes(2))
    expect(api.restoreCommandFile.mock.calls[1][2]).toEqual(api.restoreCommandFile.mock.calls[0][2])
  })

  it('does not offer restoration for a later version conflicting with the end snapshot', async () => {
    const data = fixture(); data.source = 'command_end'; data.entries[0].status = 'conflict'
    data.entries[0].note = '命令结束后有人修改，已保留当前版本。'
    api.inspectCommandRecovery.mockResolvedValue(data)
    render(<CommandRecovery sessionId="chat" commandId="cmd" />)
    fireEvent.click(screen.getByRole('button', { name: '核对文件与备份' }))
    await screen.findByText('后来有变化')
    expect(screen.queryByRole('button', { name: '恢复此文件到执行前版本' })).not.toBeInTheDocument()
  })

  it('does not notify a new view when a restore completes after unmount', async () => {
    let finish!: (value: unknown) => void
    api.restoreCommandFile.mockReturnValue(new Promise(resolve => { finish = resolve }))
    const changed = vi.fn()
    const view = render(<CommandRecovery sessionId="old" commandId="cmd" onChanged={changed} />)
    await check()
    fireEvent.click(screen.getByRole('button', { name: '恢复此文件到执行前版本' }))
    view.unmount()
    await act(async () => { finish({ request_id: 'saved', path: '', change_id: 'change', status: 'applied' }) })
    expect(api.inspectCommandRecovery).toHaveBeenCalledOnce()
    expect(changed).not.toHaveBeenCalled()
  })
})

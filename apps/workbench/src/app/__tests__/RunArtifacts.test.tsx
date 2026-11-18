import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { readRunArtifacts, type RunArtifact, type RunArtifactsPage } from '../../api/runArtifacts'
import { RuntimeApiError } from '../../api/runtime'
import { RunArtifacts } from '../chat/RunArtifacts'

vi.mock('../../api/runArtifacts', () => ({ readRunArtifacts: vi.fn() }))
const read = vi.mocked(readRunArtifacts)
const openFile = vi.fn()
const showChange = vi.fn()
const item = (changes: Partial<RunArtifact> = {}): RunArtifact => ({ receipt_id: 'change-1', source: 'file_change',
  record_state: 'applied', operation: 'create', path: 'C:/work/report.csv', expected_state: 'file', expected_sha256: 'a',
  current: { state: 'file', bytes: 42, sha256: 'a', reason: null }, matches_recorded_state: true, ...changes })
const page = (changes: Partial<RunArtifactsPage> = {}): RunArtifactsPage => ({ session_id: 'A', run_id: 'r1',
  run_status: 'succeeded', revision: 'version1', observed_at: '2026-11-08T10:00:00Z', total_paths: 1,
  observations: [item()], next_offset: null, scope: 'run_receipt_paths_only', ...changes })
const mount = (sessionId = 'A', refreshKey = '') => <><textarea aria-label="当前草稿" defaultValue="下一条还没发" />
  <RunArtifacts sessionId={sessionId} runId="r1" refreshKey={refreshKey} onOpenFile={openFile} onShowChange={showChange} /></>
const open = () => fireEvent.click(screen.getByRole('button', { name: '核对本轮文件' }))

afterEach(cleanup)
beforeEach(() => { vi.clearAllMocks(); read.mockReset(); read.mockResolvedValue(page()) })

it('reads only on demand, opens existing file/change views and preserves draft and focus', async () => {
  render(mount())
  expect(read).not.toHaveBeenCalled()
  open()
  await screen.findByText('与记录一致')
  fireEvent.click(screen.getByRole('button', { name: '打开文件' }))
  expect(openFile).toHaveBeenCalledWith('C:/work/report.csv')
  fireEvent.click(screen.getByRole('button', { name: '查看修改与撤回' }))
  expect(showChange).toHaveBeenCalledWith('change-1')
  fireEvent.keyDown(screen.getByRole('button', { name: '重新核对' }), { key: 'Escape' })
  expect(screen.queryByText('与记录一致')).toBeNull()
  expect(screen.getByRole('button', { name: '核对本轮文件' })).toHaveFocus()
  expect(screen.getByRole('textbox', { name: '当前草稿' })).toHaveValue('下一条还没发')
  open()
  await waitFor(() => expect(read).toHaveBeenCalledTimes(2))
})

it('ignores cancelled responses across A to B to A and reopening', async () => {
  let finish!: (value: RunArtifactsPage) => void
  read.mockImplementationOnce(() => new Promise(resolve => { finish = resolve }))
  const { rerender } = render(mount())
  open()
  const signal = read.mock.calls[0][4]?.signal
  rerender(mount('B'))
  expect(signal?.aborted).toBe(true)
  expect(screen.getByRole('button', { name: '核对本轮文件' })).toHaveAttribute('aria-expanded', 'false')
  rerender(mount('A'))
  open()
  await screen.findByText('与记录一致')
  await act(async () => finish(page({ observations: [item({ path: 'C:/stale.txt' })] })))
  expect(screen.queryByText('C:/stale.txt')).toBeNull()
})

it('keeps existing pages on network error, retries and rejects stale pagination', async () => {
  read.mockResolvedValueOnce(page({ total_paths: 2, next_offset: 1 }))
  render(mount())
  open()
  await screen.findByText('与记录一致')
  read.mockRejectedValueOnce(new Error('网络暂时离线'))
  fireEvent.click(screen.getByRole('button', { name: '读取更多文件' }))
  await screen.findByRole('alert')
  expect(screen.getByText('C:/work/report.csv')).toBeInTheDocument()
  read.mockRejectedValueOnce(new RuntimeApiError(409, { error: { message: '修改记录已变化，请重新读取。' } }))
  fireEvent.click(screen.getByRole('button', { name: '读取更多文件' }))
  await screen.findByText('修改记录已有变化，下面是上次读取的结果，请重新核对。')
  expect(screen.getByRole('button', { name: '读取更多文件' })).toBeDisabled()
  expect(read.mock.calls.at(-1)?.slice(0, 4)).toEqual(['A', 'r1', 1, 'version1'])
  read.mockResolvedValueOnce(page({ observations: [item({ record_state: 'undone', expected_state: 'missing',
    current: { state: 'missing', bytes: null, sha256: null, reason: null } })] }))
  fireEvent.click(screen.getByRole('button', { name: '重新核对' }))
  await screen.findByText('已撤回 · 已确认不存在')
  expect(screen.queryByRole('button', { name: '打开文件' })).toBeNull()
})

it('appends valid pages and displays unknown and changed states without a completion verdict', async () => {
  read.mockResolvedValueOnce(page({ total_paths: 2, next_offset: 1, observations: [item({ matches_recorded_state: false })] }))
  render(mount())
  open()
  await screen.findByText('与记录不同')
  read.mockResolvedValueOnce(page({ total_paths: 2, observations: [item({ path: 'C:/locked.txt', matches_recorded_state: null,
    current: { state: 'unverified', reason: 'PermissionError', bytes: null, sha256: null } })] }))
  fireEvent.click(screen.getByRole('button', { name: '读取更多文件' }))
  await screen.findByText('暂无法核对')
  expect(screen.getByText(/已读取 2 \/ 2 个路径/)).toBeInTheDocument()
  expect(screen.getByText('当前无法读取文件，请检查访问权限。')).toBeInTheDocument()
})

it('invalidates visible evidence on rollback without automatically rereading files', async () => {
  const { rerender } = render(mount())
  open()
  await screen.findByText('与记录一致')
  rerender(mount('A', 'rolled-back'))
  await screen.findByText('修改记录已有变化，下面是上次读取的结果，请重新核对。')
  expect(read).toHaveBeenCalledOnce()
})

it('distinguishes no receipts from failure and allows refresh after an error', async () => {
  read.mockRejectedValueOnce(new Error('后台暂时不可用'))
  render(mount())
  open()
  await screen.findByRole('alert')
  expect(screen.queryByText(/本轮没有可核对/)).toBeNull()
  read.mockResolvedValueOnce(page({ total_paths: 0, observations: [] }))
  fireEvent.click(screen.getByRole('button', { name: '重新核对' }))
  await screen.findByText(/本轮没有可核对的文件修改记录/)
  expect(screen.queryByRole('alert')).toBeNull()
})

it('does not expose a response belonging to another conversation', async () => {
  read.mockResolvedValueOnce(page({ session_id: 'B' }))
  render(mount())
  open()
  await screen.findByRole('alert')
  expect(screen.queryByText('C:/work/report.csv')).toBeNull()
})

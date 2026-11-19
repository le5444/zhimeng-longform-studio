import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { getChangeSet, listProjectChangeSets, summarizeChangeSet } from '../../api/changeSets'
import { getSessionFileChange, listSessionFileChanges, type SessionFileChangeDetail } from '../../api/fileChanges'
import { useChangeSets } from '../../hooks/useChangeSets'
import { useSessionFileChanges } from '../../hooks/useSessionFileChanges'
import { changeSetFixture } from '../../test/changeSetFixture'
import { ChangeHistory } from '../panel/ChangeHistory'
import { makeFile, makeSnapshot } from './fixtures'

vi.mock('../../api/changeSets', async (original) => ({ ...await original<typeof import('../../api/changeSets')>(), getChangeSet: vi.fn(), listProjectChangeSets: vi.fn() }))
vi.mock('../../api/fileChanges', () => ({ getSessionFileChange: vi.fn(), listSessionFileChanges: vi.fn(), undoSessionFileChange: vi.fn() }))

const projectChange = changeSetFixture({ origin: 'manual', status: 'applied', postDraftGateScope: 'none' })
projectChange.title = '更新预算说明'
projectChange.summary = '补齐总额'
projectChange.changes[0].relative_path = 'budget.md'
projectChange.changes[0].hunks[0].before_text = 'Total: 0'
projectChange.changes[0].hunks[0].after_text = 'Total: 378'
projectChange.created_at = '2026-10-19T03:00:00Z'
const snapshot = makeSnapshot({ chapters: [], volumes: [], files: [makeFile('budget.md')] })
snapshot.project = { ...snapshot.project, project_id: projectChange.project_id, display_name: '采购预算', root_path: 'C:/Temp/budget' }
const outside: SessionFileChangeDetail = {
  change_id: 'filechange-outside', session_id: 'chat-a', run_id: '', title: '导出 CSV',
  created_at: '2026-10-19T12:00:00+08:00', undone_at: null, status: 'applied',
  files: [{ sequence: 0, operation: 'create', path: 'C:/Temp/export.csv', target_path: null, undone_at: null }],
  previews: [], undo_available: true, undo_reason: null,
}
const notify = vi.fn(), refreshProject = vi.fn(), askConfirm = vi.fn()
function Harness({ sid = 'chat-a', focusId = null, focusRequest = 0, bound = true }: { sid?: string; focusId?: string | null; focusRequest?: number; bound?: boolean }) {
  const review = useChangeSets(bound ? snapshot.project.project_id : null)
  const session = useSessionFileChanges(sid)
  return <ChangeHistory key={`${sid}:${bound}`} project={bound ? { snapshot, review, focusId, focusRequest, onRefreshProject: refreshProject, notify, askConfirm } : null}
    session={{ sessionId: sid, review: session, focusId, focusRequest }} />
}

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(listProjectChangeSets).mockResolvedValue([summarizeChangeSet(projectChange)])
  vi.mocked(getChangeSet).mockResolvedValue(projectChange)
  vi.mocked(listSessionFileChanges).mockImplementation(async (sid) => sid === 'chat-a' ? [outside] : [])
  vi.mocked(getSessionFileChange).mockResolvedValue(outside)
})
afterEach(cleanup)

it('按真实时间合并两类修改，返回同一列表并保留来源标签', async () => {
  render(<Harness />)
  await screen.findByRole('button', { name: /导出 CSV/ })
  const rows = within(screen.getByRole('list')).getAllByRole('button')
  expect(rows).toHaveLength(2)
  expect(rows[0]).toHaveTextContent('导出 CSV')
  expect(rows[0]).toHaveTextContent('对话中的其他文件')
  expect(rows[1]).toHaveTextContent('项目文件')
  fireEvent.click(rows[1])
  expect(await screen.findByText('Total: 378')).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '全部修改' }))
  fireEvent.click(screen.getByRole('button', { name: /导出 CSV/ }))
  expect(await screen.findByRole('heading', { name: '导出 CSV' })).toBeInTheDocument()
  fireEvent.click(screen.getByRole('button', { name: '返回记录' }))
  expect(within(screen.getByRole('list')).getAllByRole('button')).toHaveLength(2)
})

it('一类读取失败仍显示另一类，并能单独重试失败来源', async () => {
  vi.mocked(listProjectChangeSets).mockRejectedValue(new Error('项目记录暂时离线'))
  render(<Harness />)
  expect(await screen.findByRole('alert')).toHaveTextContent('项目记录暂时离线')
  expect(screen.getByRole('button', { name: /导出 CSV/ })).toBeInTheDocument()
  expect(screen.queryByText('还没有文件修改记录。')).toBeNull()
  vi.mocked(listProjectChangeSets).mockResolvedValue([summarizeChangeSet(projectChange)])
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByRole('button', { name: /项目文件/ })).toBeInTheDocument()
  expect(screen.queryByRole('alert')).toBeNull()
})

it('项目详情失败留在详情，有原因和重试，成功后能继续撤回', async () => {
  vi.mocked(getChangeSet).mockRejectedValue(new Error('详情读取失败 503'))
  render(<Harness focusId={projectChange.change_set_id} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('详情读取失败 503')
  expect(screen.queryByRole('list')).toBeNull()
  vi.mocked(getChangeSet).mockResolvedValue(projectChange)
  fireEvent.click(screen.getByRole('button', { name: '重试' }))
  expect(await screen.findByText('Total: 378')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: '撤回' })).toBeEnabled()
})

it('切另一条时不展示旧详情和旧操作；返回后可再次定位同一卡片', async () => {
  const view = render(<Harness focusId={projectChange.change_set_id} focusRequest={1} />)
  await screen.findByText('Total: 378')
  let finish!: (value: typeof projectChange) => void
  const pending = new Promise<typeof projectChange>((resolve) => { finish = resolve })
  vi.mocked(getChangeSet).mockImplementation((id) => id === 'changeset-new' ? pending : Promise.resolve(projectChange))
  view.rerender(<Harness focusId="changeset-new" focusRequest={2} />)
  expect(screen.queryByText('Total: 378')).toBeNull()
  expect(screen.queryByRole('button', { name: '撤回' })).toBeNull()
  await act(async () => finish({ ...projectChange, change_set_id: 'changeset-new' }))
  await screen.findByText('Total: 378')
  fireEvent.click(screen.getByRole('button', { name: '全部修改' }))
  view.rerender(<Harness focusId="changeset-new" focusRequest={3} />)
  expect(await screen.findByText('Total: 378')).toBeInTheDocument()
})

it('详情接口返回同项目的其他记录时显示错误，不提供错误记录的撤回', async () => {
  vi.mocked(getChangeSet).mockResolvedValue({ ...projectChange, change_set_id: 'changeset-wrong' })
  render(<Harness focusId={projectChange.change_set_id} />)
  expect(await screen.findByRole('alert')).toHaveTextContent('修改详情与当前选中的记录不一致')
  expect(screen.queryByText('Total: 378')).toBeNull()
  expect(screen.queryByRole('button', { name: '撤回' })).toBeNull()
})

it('列表尚未包含指定记录时仍读该详情，404 保留为可重试错误', async () => {
  vi.mocked(listProjectChangeSets).mockResolvedValue([])
  vi.mocked(getChangeSet).mockRejectedValue(new Error('404：这条修改已不存在'))
  render(<Harness focusId="changeset-missing" />)
  expect(await screen.findByRole('alert')).toHaveTextContent('这条修改已不存在')
  expect(screen.getByRole('button', { name: '重试' })).toBeEnabled()
  expect(screen.queryByText('还没有文件修改记录。')).toBeNull()
})

it('切换对话清除其他文件详情，无项目时仍能浏览当前对话', async () => {
  const view = render(<Harness bound={false} focusId={outside.change_id} />)
  await screen.findByRole('heading', { name: '导出 CSV' })
  view.rerender(<Harness bound={false} sid="chat-b" />)
  expect(screen.queryByRole('heading', { name: '导出 CSV' })).toBeNull()
  await waitFor(() => expect(screen.getByText('还没有文件修改记录。')).toBeInTheDocument())
})

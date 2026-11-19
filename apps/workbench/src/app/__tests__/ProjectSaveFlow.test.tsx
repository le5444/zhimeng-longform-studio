import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { applyChangeSetDirect, getChangeSet, listProjectChangeSets, updateHunkDecisions } from '../../api/changeSets'
import { useChangeSets } from '../../hooks/useChangeSets'
import { changeSetFixture, changeSetProjectId, changeSetSummaryFixture } from '../../test/changeSetFixture'
import { ChangesPanel } from '../panel/Changes'
import { makeSnapshot } from './fixtures'

vi.mock('../../api/changeSets', async (load) => ({
  ...await load<typeof import('../../api/changeSets')>(),
  applyChangeSetDirect: vi.fn(), getChangeSet: vi.fn(), listProjectChangeSets: vi.fn(), updateHunkDecisions: vi.fn(),
}))
afterEach(cleanup)
const refresh = vi.fn()
let hook: ReturnType<typeof useChangeSets>
function Harness({ visible = true, projectId = changeSetProjectId }) {
  hook = useChangeSets(projectId, refresh)
  const snapshot = makeSnapshot()
  snapshot.project.project_id = projectId
  return visible ? <ChangesPanel snapshot={snapshot} review={hook} focusId={changeSetFixture().change_set_id}
    onRefreshProject={refresh} notify={vi.fn()} askConfirm={vi.fn()} /> : null
}
beforeEach(() => {
  vi.resetAllMocks()
  vi.mocked(getChangeSet).mockResolvedValue(changeSetFixture())
  vi.mocked(listProjectChangeSets).mockResolvedValue([changeSetSummaryFixture()])
  vi.mocked(applyChangeSetDirect).mockResolvedValue(changeSetFixture({ status: 'applied', decision: 'accepted', reviewRevision: 7 }))
})

it.each(['hidden', 'other-project'])('sends the entire save before the panel becomes %s', async (navigation) => {
  let finish!: (value: ReturnType<typeof changeSetFixture>) => void
  vi.mocked(applyChangeSetDirect).mockReturnValue(new Promise(resolve => { finish = resolve }))
  const view = render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: '保存' }, { timeout: 5000 }))
  await waitFor(() => expect(applyChangeSetDirect).toHaveBeenCalledExactlyOnceWith(changeSetFixture().change_set_id, changeSetFixture().review_revision))
  view.rerender(<Harness visible={false} projectId={navigation === 'other-project' ? 'other-project' : changeSetProjectId} />)
  await act(async () => finish(changeSetFixture({ status: 'applied', decision: 'accepted', reviewRevision: 7 })))
  expect(applyChangeSetDirect).toHaveBeenCalledOnce()
  expect(updateHunkDecisions).not.toHaveBeenCalled()
})

it('retains a save failure on reopening for retry', async () => {
  let fail!: (error: Error) => void
  vi.mocked(applyChangeSetDirect).mockReturnValue(new Promise((_, reject) => { fail = reject }))
  const view = render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: '保存' }, { timeout: 5000 }))
  await waitFor(() => expect(applyChangeSetDirect).toHaveBeenCalledOnce())
  view.rerender(<Harness visible={false} />)
  await act(async () => fail(new Error('The proposal changed in another window.')))
  view.rerender(<Harness />)
  expect(await screen.findByText('The proposal changed in another window.')).toBeInTheDocument()
  expect(updateHunkDecisions).not.toHaveBeenCalled()
  expect(hook.action).toBeNull()
})

it('keeps one save in flight when the panel is reopened', async () => {
  let finish!: (value: ReturnType<typeof changeSetFixture>) => void
  vi.mocked(applyChangeSetDirect).mockReturnValue(new Promise(resolve => { finish = resolve }))
  const view = render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: '保存' }, { timeout: 5000 }))
  view.rerender(<Harness visible={false} />)
  view.rerender(<Harness />)
  expect(await screen.findByRole('button', { name: '保存' })).toBeDisabled()
  await act(async () => hook.apply(true))
  expect(applyChangeSetDirect).toHaveBeenCalledOnce()
  await act(async () => finish(changeSetFixture({ status: 'applied', decision: 'accepted', reviewRevision: 7 })))
  expect(applyChangeSetDirect).toHaveBeenCalledOnce()
})

it('rejects responses for a different record even after navigation', async () => {
  let finish!: (value: ReturnType<typeof changeSetFixture>) => void
  vi.mocked(applyChangeSetDirect).mockReturnValue(new Promise(resolve => { finish = resolve }))
  const view = render(<Harness />)
  fireEvent.click(await screen.findByRole('button', { name: '保存' }, { timeout: 5000 }))
  view.rerender(<Harness visible={false} />)
  await act(async () => finish({ ...changeSetFixture(), change_set_id: 'another-record' }))
  view.rerender(<Harness />)
  expect(await screen.findByText('修改记录的返回结果对不上这次操作')).toBeInTheDocument()
  expect(hook.detail?.change_set_id).not.toBe('another-record')
})

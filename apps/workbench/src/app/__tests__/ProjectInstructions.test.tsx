import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { ProjectInstructions } from '../settings/ProjectInstructions'
import { makeFile, makeSnapshot } from './fixtures'

const api = vi.hoisted(() => ({ inspectBoundProject: vi.fn(), createProjectChangeSet: vi.fn(), applyChangeSetDirect: vi.fn(), getChangeSet: vi.fn() }))
vi.mock('../../api/projects', () => ({ inspectBoundProject: api.inspectBoundProject }))
vi.mock('../../api/changeSets', () => api)
afterEach(cleanup)
beforeEach(() => { Object.values(api).forEach(fn => fn.mockReset()) })

it('创建项目说明走版本化修改记录，回执丢失后核对原记录并打开实际文件', async () => {
  const snapshot = makeSnapshot({ files: [] })
  const onOpen = vi.fn(), onSaved = vi.fn()
  api.inspectBoundProject.mockResolvedValueOnce(snapshot).mockResolvedValueOnce(makeSnapshot({ files: [makeFile('AGENTS.md')] }))
  api.createProjectChangeSet.mockResolvedValue({ change_set_id: 'change-1', review_revision: 1, status: 'reviewing' })
  api.applyChangeSetDirect.mockRejectedValue(new Error('response lost'))
  api.getChangeSet.mockResolvedValue({ change_set_id: 'change-1', review_revision: 2, status: 'applied' })
  render(<ProjectInstructions snapshot={snapshot} onOpen={onOpen} onSaved={onSaved} />)
  fireEvent.click(screen.getByRole('button', { name: '创建 AGENTS.md' }))
  fireEvent.change(screen.getByLabelText('项目约定'), { target: { value: '# 项目约定\n保存时核对来源。\n' } })
  fireEvent.click(screen.getByRole('button', { name: '保存项目说明' }))
  await screen.findByRole('button', { name: '打开 AGENTS.md' })
  expect(api.createProjectChangeSet).toHaveBeenCalledWith(snapshot.project.project_id, expect.objectContaining({
    base_revision: snapshot.project.revision, base_fingerprint: snapshot.project.fingerprint,
    changes: [expect.objectContaining({ operation: 'create', relative_path: 'AGENTS.md', after_content: '# 项目约定\n保存时核对来源。\n' })],
  }))
  expect(api.createProjectChangeSet).toHaveBeenCalledTimes(1)
  expect(api.getChangeSet).toHaveBeenCalledWith('change-1')
  expect(onSaved).toHaveBeenCalledOnce()
  fireEvent.click(screen.getByRole('button', { name: '打开 AGENTS.md' }))
  expect(onOpen).toHaveBeenCalledWith('AGENTS.md')
})

it('版本冲突不丢正文，并恢复编辑以重新核对保存', async () => {
  api.inspectBoundProject.mockResolvedValue(makeSnapshot({ files: [] }))
  api.createProjectChangeSet.mockResolvedValue({ change_set_id: 'c1', review_revision: 1, status: 'reviewing' })
  api.applyChangeSetDirect.mockRejectedValue(new Error('版本冲突'))
  api.getChangeSet.mockResolvedValue({ change_set_id: 'c1', review_revision: 2, status: 'conflicted' })
  render(<ProjectInstructions snapshot={makeSnapshot({ files: [] })} onOpen={vi.fn()} />)
  fireEvent.click(screen.getByRole('button', { name: '创建 AGENTS.md' }))
  fireEvent.change(screen.getByLabelText('项目约定'), { target: { value: '保留的约定' } })
  fireEvent.click(screen.getByRole('button', { name: '保存项目说明' }))
  await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('版本冲突'))
  expect(screen.getByLabelText('项目约定')).toHaveValue('保留的约定')
  expect(screen.getByLabelText('项目约定')).toBeEnabled()
})

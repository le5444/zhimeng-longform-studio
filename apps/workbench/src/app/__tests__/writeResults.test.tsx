import { describe, expect, it, vi } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { AgentActivity } from '../../api/agent'
import { actionWords } from '../lib/words'
import { StepList } from '../chat/Steps'
import { writeResults } from '../chat/writeResults'

const output = {
  applied: true, incomplete: true,
  files: [{ relative_path: 'report.csv', operation: 'created' }, { relative_path: 'notes.md', operation: 'unchanged' }],
  outside_open_folder: { status: 'unconfirmed', files: [{ path: 'C:/exports/copy.csv', operation: 'unconfirmed' }] },
  unconfirmed_paths: ['C:/exports/copy.csv'],
}

describe('file write receipts', () => {
  it('keeps each destination state and does not duplicate unconfirmed paths', () => {
    expect(writeResults(output)).toEqual([
      { path: 'report.csv', status: 'saved' },
      { path: 'notes.md', status: 'unchanged' },
      { path: 'C:/exports/copy.csv', status: 'unconfirmed', external: true },
    ])
  })

  it('does not treat an external failure as committed just because the project succeeded', () => {
    expect(writeResults({ ...output, outside_open_folder: { applied: false, files: [{ path: 'copy.csv', operation: 'create' }] } })[2].status).toBe('unconfirmed')
    expect(writeResults({ applied: false, status: 'rolled_back', relative_path: 'report.csv', operation: 'created' })).toEqual([{ path: 'report.csv', status: 'rolled_back' }])
    expect(writeResults({ outside_open_folder: { applied: false, status: 'rolled_back', change_id: 'external-undo', files: [{ path: 'C:/exports/copy.csv', operation: 'create' }] } })).toEqual([
      { path: 'C:/exports/copy.csv', status: 'rolled_back', external: true, changeId: 'external-undo' },
    ])
    expect(writeResults({ outside_open_folder: { applied: true, change_id: 'partial-undo', files: [
      { path: 'C:/exports/remaining.csv', operation: 'create' },
      { path: 'C:/exports/undone.csv', operation: 'create', status: 'rolled_back' },
    ] } })).toEqual([
      { path: 'C:/exports/remaining.csv', status: 'saved', external: true, changeId: 'partial-undo' },
      { path: 'C:/exports/undone.csv', status: 'rolled_back', external: true, changeId: 'partial-undo' },
    ])
  })

  it('keeps a failed batch distinct from a successful write and opens the confirmed result', () => {
    const activities = [
      { run_id: 'r1', sequence: 1, kind: 'tool_call', payload: { tool_call_id: 't1', tool_name: 'write_file', arguments: { files: [{ path: 'missing.csv' }, { path: 'report.csv' }] } } },
      { run_id: 'r1', sequence: 2, kind: 'tool_result', payload: { tool_call_id: 't1', status: 'failed', output } },
    ] as unknown as AgentActivity[]
    expect(actionWords(activities, null)[0]).toMatchObject({ state: 'failed', text: '保存未完成 · 已保存 1 个文件', targetPath: 'report.csv' })
    const missing = structuredClone(activities)
    if (missing[1].kind === 'tool_result') missing[1].payload.output = {}
    expect(actionWords(missing, null)[0]).toMatchObject({ text: '保存未完成', targetPath: undefined })
  })

  it('opens project files and the external receipt while keeping unconfirmed paths explicit', async () => {
    const user = userEvent.setup()
    const onOpenFile = vi.fn()
    const onShowChange = vi.fn()
    render(<StepList onOpenFile={onOpenFile} onShowChange={onShowChange} rows={[{
      key: 'batch', kind: 'write', state: 'failed', outcome: 'failed',
      text: '保存未完成 · 已保存 1 个文件', fileResults: [...writeResults(output),
        { path: 'C:/exports/saved.csv', status: 'saved', external: true, changeId: 'filechange-1' }],
    }]} />)
    await user.click(screen.getByRole('button', { name: '保存未完成 · 已保存 1 个文件' }))
    await user.click(screen.getByRole('button', { name: 'report.csv' }))
    expect(onOpenFile).toHaveBeenLastCalledWith('report.csv')
    await user.click(screen.getByRole('button', { name: 'notes.md' }))
    expect(onOpenFile).toHaveBeenLastCalledWith('notes.md')
    expect(screen.getByText('C:/exports/copy.csv')).toBeVisible()
    expect(screen.getByText('未确认')).toBeVisible()
    expect(screen.queryByRole('button', { name: 'C:/exports/copy.csv' })).not.toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'C:/exports/saved.csv' }))
    expect(onShowChange).toHaveBeenLastCalledWith('filechange-1')
  })
})

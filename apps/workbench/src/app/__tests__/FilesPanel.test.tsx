import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectFileContent } from '../../api/projects'
import { FilesPanel } from '../panel/Files'
import { A } from '../text'
import { fileDraftKey, writeFileDraft } from '../lib/fileDrafts'
import { makeFile, makeSnapshot } from './fixtures'
import { changeSetFixture, changeSetSummaryFixture } from '../../test/changeSetFixture'

const api = vi.hoisted(() => ({ readBoundProjectFile: vi.fn(), createProjectChangeSet: vi.fn(), applyChangeSetDirect: vi.fn(), getChangeSet: vi.fn(), listProjectChangeSets: vi.fn() }))
vi.mock('../../api/projects', () => ({ readBoundProjectFile: api.readBoundProjectFile }))
vi.mock('../../api/changeSets', async (original) => ({ ...await original<typeof import('../../api/changeSets')>(), createProjectChangeSet: api.createProjectChangeSet, applyChangeSetDirect: api.applyChangeSetDirect, getChangeSet: api.getChangeSet, listProjectChangeSets: api.listProjectChangeSets }))
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.localStorage.clear() })

const files = [makeFile('alpha.txt'), makeFile('beta.txt')]
const snapshot = makeSnapshot({ files, chapters: [], volumes: [], project: { project_id: 'p1', display_name: '普通项目', root_path: 'D:/work', format: 'folder', revision: 3, fingerprint: 'fp-3' } })
async function textHash(text: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text))
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, '0')).join('')
}
function content(fileId: string, text?: string): ProjectFileContent {
  const file = files.find((item) => item.file_id === fileId)!
  return { schema: 'zhimeng.project-file-content.v1', read_only: true, project_id: 'p1', snapshot_revision: 3, project_fingerprint: 'fp-3', file, etag: file.sha256, content: text ?? `original ${file.relative_path}` }
}
function setup() {
  const props = { projectId: 'p1', snapshot, openPath: 'alpha.txt', onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
  const view = render(<FilesPanel {...props} />)
  return { ...props, show: (path: string | null) => view.rerender(<FilesPanel {...props} openPath={path} />),
    updateSnapshot: (next: typeof snapshot) => view.rerender(<FilesPanel {...props} snapshot={next} />) }
}

describe('文件编辑不串稿', () => {
  beforeEach(() => {
    window.localStorage.clear()
    Object.values(api).forEach((fn) => fn.mockReset())
    api.readBoundProjectFile.mockImplementation(async (_projectId, fileId) => content(fileId))
    api.createProjectChangeSet.mockResolvedValue({ change_set_id: 'cs-draft', review_revision: 1, changes: [{ proposed_after_sha256: 'saved-hash' }] })
    api.applyChangeSetDirect.mockResolvedValue({})
  })

  it('文档提取显示范围、可复制引用，不能把提取文字保存到原二进制文件', async () => {
    const source = content('file:alpha.txt', 'A1\t纸张\nB1\t25 〔公式缓存，未重新计算〕')
    source.extraction = { format: 'xlsx', parser: 'openxml-text-v1', read_only: true, source_sha256: source.etag,
      source_bytes: 1024, warnings: ['公式缓存可能过期；不重新计算。'] }
    api.readBoundProjectFile.mockResolvedValue(source)
    const onQuote = vi.fn()
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    render(<FilesPanel projectId="p1" snapshot={snapshot} openPath="alpha.txt" onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} onQuote={onQuote} />)
    const region = await screen.findByRole('region')
    expect(screen.getByRole('note', { name: '文档提取范围' })).toHaveTextContent('公式缓存可能过期')
    expect(screen.queryByRole('button', { name: A.files.edit })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: '复制文件内容' }))
    expect(writeText).toHaveBeenCalledWith(source.content)
    const text = region.querySelector('code')!.firstChild!
    const range = document.createRange()
    range.setStart(text, 0)
    range.setEnd(text, 5)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    fireEvent(document, new Event('selectionchange'))
    fireEvent.click(screen.getByRole('button', { name: '引用选中内容' }))
    expect(onQuote).toHaveBeenCalledWith(expect.objectContaining({ source: 'preview', text: source.content.slice(0, 5), location: undefined }))
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it.each(['retry', 'remount', 'edited', 'external'])('保存和查询连续断线后沿原记录恢复：%s', async (mode) => {
    const loaded = content('file:alpha.txt', 'original alpha.txt\r\n')
    api.readBoundProjectFile.mockResolvedValue(loaded)
    let created: ReturnType<typeof changeSetFixture>
    api.createProjectChangeSet.mockImplementation(async (_projectId, request) => {
      const candidate = changeSetFixture({ origin: 'manual', reviewRevision: 2 })
      created = { ...candidate, project_id: 'p1', base_revision: loaded.snapshot_revision, base_fingerprint: loaded.project_fingerprint,
        changes: [{ ...candidate.changes[0], file_id: loaded.file.file_id, proposed_after_sha256: await textHash(request.changes[0].after_content.replace(/\r?\n/g, '\r\n')) }] }
      return created
    })
    api.applyChangeSetDirect.mockRejectedValue(new Error('Lost apply reply'))
    api.listProjectChangeSets.mockRejectedValueOnce(new Error('Recovery connection also lost'))
    api.getChangeSet.mockImplementation(async () => ({ ...created, status: 'applied' }))
    let page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Saved draft awaiting confirmation\n' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await screen.findByText(/Recovery connection also lost/)
    await waitFor(() => expect(screen.getByRole('button', { name: A.files.save })).toBeEnabled())
    expect(screen.getByRole('status')).toHaveTextContent('保存待确认')
    if (mode === 'remount') {
      cleanup()
      page = setup()
      expect(screen.getByRole('textbox')).toHaveValue('Saved draft awaiting confirmation\n')
      expect(screen.getByRole('status')).toHaveTextContent('保存待确认')
    }
    if (mode === 'edited' || mode === 'external') {
      fireEvent.change(screen.getByRole('textbox'), { target: { value: 'Newer unsaved draft' } })
      api.readBoundProjectFile.mockResolvedValue({ ...loaded, file: { ...loaded.file, sha256: created!.changes[0].proposed_after_sha256! }, snapshot_revision: 4, project_fingerprint: 'fp-4', content: 'Saved draft awaiting confirmation' })
      if (mode === 'external') api.readBoundProjectFile.mockResolvedValue({ ...loaded, file: { ...loaded.file, sha256: 'external-hash' }, content: 'Changed by another task' })
    }
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    if (mode === 'external') {
      await screen.findByText(/文件后来又有变化/)
      expect(screen.getByRole('textbox')).toHaveValue('Newer unsaved draft')
      expect(page.onSaved).not.toHaveBeenCalled()
      expect(api.createProjectChangeSet).toHaveBeenCalledOnce()
      expect(api.applyChangeSetDirect).toHaveBeenCalledOnce()
      return
    }
    await waitFor(() => expect(page.onSaved).toHaveBeenCalledOnce())
    expect(api.createProjectChangeSet).toHaveBeenCalledOnce()
    expect(api.applyChangeSetDirect).toHaveBeenCalledOnce()
    expect(api.getChangeSet).toHaveBeenCalledWith(created!.change_set_id)
    if (mode === 'edited') {
      expect(screen.getByRole('textbox')).toHaveValue('Newer unsaved draft')
      expect(screen.getByRole('status')).toHaveTextContent('未保存')
      cleanup()
      page = setup()
      expect(screen.getByRole('textbox')).toHaveValue('Newer unsaved draft')
      api.applyChangeSetDirect.mockResolvedValue({})
      fireEvent.click(screen.getByRole('button', { name: A.files.save }))
      await waitFor(() => expect(page.onSaved).toHaveBeenCalledOnce())
      expect(api.createProjectChangeSet).toHaveBeenLastCalledWith('p1', expect.objectContaining({ base_revision: 4, base_fingerprint: 'fp-4',
        changes: [expect.objectContaining({ after_content: 'Newer unsaved draft' })] }))
    }
  })

  it('行号跳转保留原文和CRLF，重复定位不重新读取或写入', async () => {
    const original = 'first\r\n\tsecond\r\nthird\r\n'
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', original))
    const props = { projectId: 'p1', snapshot, openPath: 'alpha.txt', onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
    const view = render(<FilesPanel {...props} focus={{ location: { line: 2, column: 2 }, request: 1 }} />)
    const region = await screen.findByRole('region', { name: '文件内容 alpha.txt' })
    expect(region.querySelector('mark')).toHaveTextContent('second')
    expect(region.querySelector('code')?.textContent).toBe(original)
    view.rerender(<FilesPanel {...props} focus={{ location: { line: 3 }, request: 2 }} />)
    expect(region.querySelector('mark')).toHaveTextContent('third')
    expect(api.readBoundProjectFile).toHaveBeenCalledOnce()
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it('引用原文选段带完整内容和行列，键盘移到操作栏仍保留选区', async () => {
    const original = 'first\r\n\tsecond line\r\nthird'
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', original))
    const onQuote = vi.fn()
    render(<FilesPanel projectId="p1" snapshot={snapshot} openPath="alpha.txt" onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} onQuote={onQuote} />)
    const region = await screen.findByRole('region')
    const text = region.querySelector('code')!.firstChild!
    const range = document.createRange()
    range.setStart(text, original.indexOf('second'))
    range.setEnd(text, original.indexOf('second') + 'second line'.length)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    fireEvent(document, new Event('selectionchange'))
    const action = screen.getByRole('button', { name: '加入对话' })
    act(() => action.focus())
    fireEvent(document, new Event('selectionchange'))
    fireEvent.click(action)
    expect(onQuote).toHaveBeenCalledWith({ projectId: 'p1', path: 'alpha.txt', text: 'second line', source: 'original',
      location: { line: 2, column: 2, endLine: 2, endColumn: 12 } })
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it('引用未保存草稿不写文件，不截断，切文件不会沿用旧选段', async () => {
    const onQuote = vi.fn()
    const props = { projectId: 'p1', snapshot, openPath: 'alpha.txt', onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn(), onQuote }
    const view = render(<FilesPanel {...props} />)
    await screen.findByRole('region')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    const editor = screen.getByRole('textbox') as HTMLTextAreaElement
    const draft = '未保存的资料\n' + '完整段落\n'.repeat(800)
    fireEvent.change(editor, { target: { value: draft } })
    fireEvent.click(screen.getByRole('button', { name: '引用当前草稿' }))
    expect(onQuote).toHaveBeenLastCalledWith({ projectId: 'p1', path: 'alpha.txt', text: draft, source: 'draft' })
    expect(editor).toHaveValue(draft)
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
    view.rerender(<FilesPanel {...props} openPath="beta.txt" />)
    await screen.findByRole('region', { name: '文件内容 beta.txt' })
    fireEvent.click(screen.getByRole('button', { name: '引用文件到对话' }))
    expect(onQuote).toHaveBeenLastCalledWith({ projectId: 'p1', path: 'beta.txt' })
  })

  it('预览选段引用可见文字，不编造源文件行号或夹带其他区域', async () => {
    const file = makeFile('docs/notes.md')
    api.readBoundProjectFile.mockResolvedValue({ ...content('file:alpha.txt'), file, content: '# Notes\n\n**target** text\n\n```txt\n  keep code\n```' })
    const onQuote = vi.fn()
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [file] }} openPath={file.relative_path} onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} onQuote={onQuote} />)
    const target = await screen.findByText('target')
    const range = document.createRange()
    range.selectNodeContents(target)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    fireEvent(document, new Event('selectionchange'))
    fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
    expect(onQuote).toHaveBeenCalledWith({ projectId: 'p1', path: file.relative_path, text: 'target', source: 'preview', location: undefined })
    act(() => (document.activeElement as HTMLElement).blur())
    range.selectNodeContents(target.closest('.zm-file-prose')!)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    fireEvent(document, new Event('selectionchange'))
    fireEvent.click(screen.getByRole('button', { name: '加入对话' }))
    expect(onQuote.mock.lastCall![0].text).toContain('  keep code\n')
    expect(onQuote.mock.lastCall![0].text).not.toContain('复制代码')
    window.getSelection()!.removeAllRanges()
    fireEvent(document, new Event('selectionchange'))
    expect(screen.queryByRole('group', { name: '文件选段' })).not.toBeInTheDocument()
  })

  it('Markdown行号打开原文，预览可切回，再次点击同一位置恢复原文定位', async () => {
    const file = makeFile('docs/notes.md')
    const original = '# Title\n\n**target**\n'
    api.readBoundProjectFile.mockResolvedValue({ ...content('file:alpha.txt'), file, content: original })
    const props = { projectId: 'p1', snapshot: { ...snapshot, files: [file] }, openPath: file.relative_path, onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
    const view = render(<FilesPanel {...props} focus={{ location: { line: 3 }, request: 1 }} />)
    const region = await screen.findByRole('region', { name: `文件内容 ${file.relative_path}` })
    expect(region.querySelector('mark')?.textContent).toBe('**target**')
    fireEvent.click(screen.getByRole('button', { name: '预览' }))
    expect(screen.getByRole('heading', { name: 'Title' })).toBeInTheDocument()
    view.rerender(<FilesPanel {...props} focus={{ location: { line: 3 }, request: 2 }} />)
    expect(screen.getByRole('region').querySelector('mark')?.textContent).toBe('**target**')
  })

  it('跳转按未保存草稿定位且不覆盖内容，继续输入不会反复移动选区', async () => {
    const original = 'first\r\nsecond\r\nthird\r\n'
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', original))
    const props = { projectId: 'p1', snapshot, openPath: 'alpha.txt', onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
    const view = render(<FilesPanel {...props} />)
    await screen.findByRole('region')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    const editor = screen.getByRole('textbox') as HTMLTextAreaElement
    fireEvent.change(editor, { target: { value: 'new first\nchanged second\nthird\n' } })
    view.rerender(<FilesPanel {...props} focus={{ location: { line: 2 }, request: 1 }} />)
    expect(editor.value.slice(editor.selectionStart, editor.selectionEnd)).toBe('changed second')
    expect(editor.value).toBe('new first\nchanged second\nthird\n')
    fireEvent.change(editor, { target: { value: 'new first\nchanged second\nthird\nnext' } })
    expect(editor.selectionStart).toBe(editor.value.length)
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
    expect(screen.getByRole('status')).toHaveTextContent('未保存')
  })

  it('过期行号给出明确反馈，不跳到另一个位置或阻止继续编辑', async () => {
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', 'one\ntwo'))
    render(<FilesPanel projectId="p1" snapshot={snapshot} openPath="alpha.txt" focus={{ location: { line: 80 }, request: 1 }} onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} />)
    await screen.findByText('引用的第 80 行已超出当前文件，内容可能已变化。')
    expect(screen.getByRole('region').querySelector('mark')).toBeNull()
    expect(screen.getByRole('button', { name: A.files.edit })).toBeEnabled()
  })

  it('预览保留空行与缩进，行号独立显示；复制得到逐字原文', async () => {
    const original = 'def run():\r\n\treturn 42\r\n\r\n# end\r\n'
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', original))
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    setup()
    const region = await screen.findByRole('region', { name: '文件内容 alpha.txt' })
    fireEvent.click(screen.getByRole('button', { name: '自动换行' }))
    expect(region).toHaveAttribute('tabindex', '0')
    expect(region.querySelector('code')?.textContent).toBe(original)
    expect(region.querySelector('[aria-hidden="true"]')).toHaveTextContent('1 2 3 4')
    expect(screen.getByText('4 行')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制文件内容' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(original))
    expect(await screen.findByRole('button', { name: '已复制文件内容' })).toBeInTheDocument()
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it('长文本默认换行，切换横滚行号及编辑软换行都不改变正文', async () => {
    const original = 'first '.repeat(80) + '\nsecond line'
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', original))
    setup()
    const region = await screen.findByRole('region', { name: '文件内容 alpha.txt' })
    const toggle = screen.getByRole('button', { name: '自动换行' })
    expect(toggle).toHaveAttribute('aria-pressed', 'true')
    expect(region.querySelector('.zm-file-line-numbers')).not.toBeInTheDocument()
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-pressed', 'false')
    expect(region.querySelector('.zm-file-line-numbers')).toHaveTextContent('1 2')
    expect(region.querySelector('code')?.textContent).toBe(original)
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    const editor = screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') })
    expect(editor).toHaveAttribute('wrap', 'off')
    fireEvent.click(toggle)
    expect(editor).toHaveAttribute('wrap', 'soft')
    expect(editor).toHaveValue(original)
    expect(screen.getByRole('status')).toHaveTextContent('编辑中')
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it('编辑状态和行数随草稿更新，复制当前内容不会保存或清掉草稿', async () => {
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText } })
    setup()
    await screen.findByText('original alpha.txt')
    expect(screen.getByRole('status')).toHaveTextContent('预览')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    expect(screen.getByRole('status')).toHaveTextContent('编辑中')
    const editor = screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') })
    fireEvent.change(editor, { target: { value: 'first\n\nthird' } })
    expect(screen.getByRole('status')).toHaveTextContent('未保存')
    expect(screen.getByText('3 行')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '复制当前内容' }))
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('first\n\nthird'))
    expect(editor).toHaveValue('first\n\nthird')
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
  })

  it('复制失败显示原因并保留正文与编辑入口', async () => {
    Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockRejectedValue(new Error('not allowed')) } })
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: '复制文件内容' }))
    await waitFor(() => expect(page.notify).toHaveBeenCalledWith('danger', '复制没有成功，请选中文字后复制。'))
    expect(screen.getByText('original alpha.txt')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: A.files.edit })).toBeInTheDocument()
  })

  it('空文件有明确提示且保持可编辑，长路径完整保留', async () => {
    const path = 'a very long folder/另一个比较长的文件夹/empty.py'
    const file = makeFile(path, { kind: 'asset' })
    api.readBoundProjectFile.mockResolvedValue({ ...content('file:alpha.txt'), file, content: '' })
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [file] }} openPath={path} onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} />)
    expect(await screen.findByText('空文件')).toBeInTheDocument()
    expect(screen.getByText(path).closest('[title]')).toHaveAttribute('title', path)
    expect(screen.getByRole('heading', { name: 'empty.py' })).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    expect(screen.getByRole('textbox', { name: A.files.editorLabel(path) })).toHaveValue('')
  })

  it.each(['app.py', 'web/app.js', 'config.json', 'data/sales.csv', 'Makefile', '.env'])('普通文件%s即使标为asset也按实际文本内容读取、编辑与保存', async (path) => {
    const file = makeFile(path, { kind: 'asset' })
    const loaded = { ...content('file:alpha.txt'), file, content: 'price,qty\n12,3\n' }
    api.readBoundProjectFile.mockResolvedValue(loaded)
    const props = { projectId: 'p1', snapshot: { ...snapshot, files: [file] }, openPath: path, onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
    render(<FilesPanel {...props} />)
    expect(await screen.findByText('price,qty 12,3')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel(path) }), { target: { value: 'price,qty\n12,4\n' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(api.createProjectChangeSet).toHaveBeenCalledWith('p1', expect.objectContaining({ base_revision: 3, base_fingerprint: 'fp-3',
      changes: [expect.objectContaining({ file_id: file.file_id, after_content: 'price,qty\n12,4\n' })] })))
    expect(props.onOpenPath).not.toHaveBeenCalled()
  })


  it('保存丢回执后查回原记录，实际已落盘不重复创建或重写', async () => {
    const loaded = content('file:alpha.txt')
    let created: ReturnType<typeof changeSetFixture> | undefined
    api.createProjectChangeSet.mockImplementation(async (_projectId, request) => {
      const candidate = changeSetFixture({ origin: 'manual', reviewRevision: 2 })
      created = { ...candidate, project_id: 'p1', base_revision: loaded.snapshot_revision, base_fingerprint: loaded.project_fingerprint,
        changes: [{ ...candidate.changes[0], file_id: loaded.file.file_id, proposed_after_sha256: await textHash(request.changes[0].after_content) }] }
      return created
    })
    api.applyChangeSetDirect.mockRejectedValue(new Error('Lost response'))
    api.listProjectChangeSets.mockImplementation(async () => [changeSetSummaryFixture({ origin: 'manual' })])
    api.getChangeSet.mockImplementation(async () => ({ ...created!, status: 'applied' }))
    const props = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '恢复后的内容' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(props.onSaved).toHaveBeenCalledOnce())
    expect(api.createProjectChangeSet).toHaveBeenCalledOnce()
    expect(api.applyChangeSetDirect).toHaveBeenCalledOnce()
    expect(api.getChangeSet).toHaveBeenCalledWith(created!.change_set_id)
    expect(props.notify).toHaveBeenCalledWith('ok', A.files.saved('alpha.txt'))
  })

  it('保存丢回执但记录不匹配时保留草稿，不拿别人的记录冒充结果', async () => {
    api.createProjectChangeSet.mockResolvedValue({ change_set_id: 'cs-other', review_revision: 2, changes: [{ proposed_after_sha256: 'other-hash' }] })
    api.applyChangeSetDirect.mockRejectedValue(new Error('Lost response'))
    api.listProjectChangeSets.mockResolvedValue([{ change_set_id: 'cs-other' }])
    api.getChangeSet.mockResolvedValue({ project_id: 'another-project', origin: 'manual', changes: [] })
    const props = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox'), { target: { value: '仍未确认的草稿' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await screen.findByText(/原保存记录的内容已经不同/)
    expect(screen.getByRole('textbox')).toHaveValue('仍未确认的草稿')
    expect(props.onSaved).not.toHaveBeenCalled()
    expect(api.createProjectChangeSet).toHaveBeenCalledOnce()
  })

  it('读取失败保留具体原因并可重试，晚到的A结果不盖住B', async () => {
    api.readBoundProjectFile.mockRejectedValueOnce(new Error('文件正在被外部程序占用'))
    const page = setup()
    expect(await screen.findByRole('alert')).toHaveTextContent('文件正在被外部程序占用')
    let finish!: (value: ProjectFileContent) => void
    api.readBoundProjectFile.mockImplementationOnce(() => new Promise((resolve) => { finish = resolve }))
    fireEvent.click(screen.getByRole('button', { name: '重新读取' }))
    page.show('beta.txt')
    await screen.findByText('original beta.txt')
    await act(async () => finish(content('file:alpha.txt', 'A 的迟到结果')))
    expect(screen.queryByText('A 的迟到结果')).not.toBeInTheDocument()
    expect(page.notify).not.toHaveBeenCalled()
  })

  it('二进制由后台识别并说明原因，不提供会破坏文件的编辑入口', async () => {
    const file = makeFile('picture.png', { kind: 'asset' })
    api.readBoundProjectFile.mockRejectedValue(new Error('这是二进制文件，无法作为文本显示'))
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [file] }} openPath="picture.png" onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} />)
    expect(await screen.findByRole('alert')).toHaveTextContent('二进制文件')
    expect(screen.queryByRole('button', { name: A.files.edit })).not.toBeInTheDocument()
  })

  it('文件刚新建或已移动时说清路径并提供刷新，不默默退回文件树', () => {
    const props = { projectId: 'p1', snapshot, openPath: 'new/report.csv', onOpenPath: vi.fn(), onSaved: vi.fn(), notify: vi.fn() }
    render(<FilesPanel {...props} />)
    expect(screen.getByRole('status')).toHaveTextContent('new/report.csv')
    fireEvent.click(screen.getByRole('button', { name: '刷新文件列表' }))
    expect(props.onSaved).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: A.files.back }))
    expect(props.onOpenPath).toHaveBeenCalledWith(null)
  })

  it('文件内Markdown相对链接从文档目录解析，能跳到同项目代码', async () => {
    const file = makeFile('docs/README.md')
    api.readBoundProjectFile.mockResolvedValue({ ...content('file:alpha.txt'), file, content: '[执行脚本](../src/app.py#L2)' })
    const open = vi.fn()
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [file, makeFile('src/app.py')] }} openPath="docs/README.md" onOpenPath={open} onSaved={vi.fn()} notify={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: '执行脚本' }))
    expect(open).toHaveBeenCalledWith('src/app.py', { line: 2 })
  })

  it('文档中的章节锚点先打开相邻文档，不把锚点误当不存在的文件名', async () => {
    const file = makeFile('docs/README.md')
    api.readBoundProjectFile.mockResolvedValue({ ...content('file:alpha.txt'), file, content: '[用法](guide.md#usage)' })
    const open = vi.fn()
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [file, makeFile('docs/guide.md')] }} openPath="docs/README.md" onOpenPath={open} onSaved={vi.fn()} notify={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: '用法' }))
    expect(open).toHaveBeenCalledWith('docs/guide.md')
  })

  it('编辑A后点B，B读取自己的文件；保存B仍保留A草稿及其原始版本', async () => {
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'draft alpha' } })
    page.show('beta.txt')
    await screen.findByText('original beta.txt')
    expect(screen.queryByDisplayValue('draft alpha')).not.toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('beta.txt') }), { target: { value: 'draft beta' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(api.createProjectChangeSet).toHaveBeenCalledWith('p1', expect.objectContaining({ changes: [expect.objectContaining({ file_id: 'file:beta.txt', after_content: 'draft beta' })] })))
    await waitFor(() => expect(page.onSaved).toHaveBeenCalledOnce())
    page.show('alpha.txt')
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('draft alpha')
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(api.createProjectChangeSet).toHaveBeenLastCalledWith('p1', expect.objectContaining({ base_revision: 3, base_fingerprint: 'fp-3', changes: [expect.objectContaining({ file_id: 'file:alpha.txt', after_content: 'draft alpha' })] })))
  })

  it('回到文件列表再打开仍保留草稿；明确不改了才清掉并重读文件', async () => {
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'draft alpha' } })
    page.show(null)
    page.show('alpha.txt')
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('draft alpha')
    api.readBoundProjectFile.mockResolvedValue(content('file:alpha.txt', 'changed outside'))
    fireEvent.click(screen.getByRole('button', { name: A.files.cancel }))
    await screen.findByText('changed outside')
    page.show('beta.txt')
    page.show('alpha.txt')
    await screen.findByText('changed outside')
    expect(screen.queryByDisplayValue('draft alpha')).not.toBeInTheDocument()
  })

  it('外部更新后保存失败保留原稿，切换文件回来也仍带旧版本做冲突检查', async () => {
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'draft alpha' } })
    page.updateSnapshot({ ...snapshot, project: { ...snapshot.project, revision: 4, fingerprint: 'fp-4' }, files: [{ ...files[0], sha256: 'new-sha' }, files[1]] })
    api.createProjectChangeSet.mockRejectedValue(new Error('文件已有更新'))
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(page.notify).toHaveBeenCalledWith('danger', '文件已有更新'))
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
    page.show('beta.txt')
    await screen.findByText('original beta.txt')
    page.show('alpha.txt')
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('draft alpha')
    expect(api.createProjectChangeSet).toHaveBeenCalledWith('p1', expect.objectContaining({ base_revision: 3, base_fingerprint: 'fp-3' }))
  })

  it('A保存途中切走再回来编辑，新草稿不会被旧保存响应清掉', async () => {
    let complete!: (value: unknown) => void
    api.applyChangeSetDirect.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'first draft' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(api.applyChangeSetDirect).toHaveBeenCalledOnce())
    page.show('beta.txt')
    await screen.findByText('original beta.txt')
    page.show('alpha.txt')
    fireEvent.change(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'newer draft' } })
    await act(async () => { complete({}) })
    page.show('beta.txt')
    await screen.findByText('original beta.txt')
    page.show('alpha.txt')
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('newer draft')
  })

  it('关闭面板后重新挂载恢复草稿和基准版本，保存成功才清除本机副本', async () => {
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'reload draft\n中文' } })
    cleanup()
    const page = setup()
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('reload draft\n中文')
    expect(api.createProjectChangeSet).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(page.onSaved).toHaveBeenCalledOnce())
    expect(api.createProjectChangeSet).toHaveBeenCalledWith('p1', expect.objectContaining({ base_revision: 3, base_fingerprint: 'fp-3' }))
    cleanup()
    setup()
    await screen.findByText('original alpha.txt')
    expect(screen.queryByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).not.toBeInTheDocument()
  })

  it('刷新期间外部替换了同路径文件也保留草稿，保存仍带旧文件和版本做冲突核对', async () => {
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'my unsaved draft' } })
    cleanup()
    const changed = makeFile('alpha.txt', { file_id: 'new-file-id', sha256: 'changed-sha' })
    render(<FilesPanel projectId="p1" snapshot={{ ...snapshot, files: [changed] }} openPath="alpha.txt" onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} />)
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('my unsaved draft')
    expect(screen.getByText(/文件已有外部修改/)).toBeInTheDocument()
    api.createProjectChangeSet.mockRejectedValue(new Error('文件已有更新'))
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await screen.findByRole('alert')
    expect(api.createProjectChangeSet).toHaveBeenCalledWith('p1', expect.objectContaining({ base_fingerprint: 'fp-3', changes: [expect.objectContaining({ file_id: 'file:alpha.txt' })] }))
    expect(api.applyChangeSetDirect).not.toHaveBeenCalled()
  })

  it('浏览器拒绝持久保存时明确告知，当前输入及复制保存仍可用', async () => {
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'preserved in page' } })
    expect(screen.getByRole('alert')).toHaveTextContent('草稿仅保存在当前页面')
    expect(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('preserved in page')
    expect(screen.getByRole('button', { name: '复制当前内容' })).toBeEnabled()
    expect(screen.getByRole('button', { name: A.files.save })).toBeEnabled()
  })

  it('不改了会清理本机草稿，同一路径在另一个项目也不会读到旧草稿', async () => {
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'private p1 draft' } })
    cleanup()
    render(<FilesPanel projectId="p2" snapshot={{ ...snapshot, project: { ...snapshot.project, project_id: 'p2' } }} openPath="alpha.txt" onOpenPath={vi.fn()} onSaved={vi.fn()} notify={vi.fn()} />)
    await screen.findByText('original alpha.txt')
    expect(screen.queryByDisplayValue('private p1 draft')).not.toBeInTheDocument()
    cleanup()
    setup()
    fireEvent.click(await screen.findByRole('button', { name: A.files.cancel }))
    await screen.findByText('original alpha.txt')
    cleanup()
    setup()
    await screen.findByText('original alpha.txt')
    expect(screen.queryByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).not.toBeInTheDocument()
  })

  it('旧保存回包不能删除整页重新挂载后写下的新草稿', async () => {
    let complete!: (value: unknown) => void
    api.applyChangeSetDirect.mockImplementation(() => new Promise((resolve) => { complete = resolve }))
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    fireEvent.change(screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'submitted draft' } })
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(api.applyChangeSetDirect).toHaveBeenCalledOnce())
    cleanup()
    setup()
    fireEvent.change(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') }), { target: { value: 'new page draft' } })
    await act(async () => { complete({}) })
    cleanup()
    setup()
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('new page draft')
  })

  it.each(['cancel', 'revert'])('旧页面 %s 草稿时不删除另一页面后来保存的新稿', async (action) => {
    setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    const editor = screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') })
    fireEvent.change(editor, { target: { value: 'old page draft' } })
    // Another page's local draft arrives while this editor is still mounted.
    writeFileDraft(fileDraftKey('p1', 'alpha.txt'), { base: content('file:alpha.txt'), text: 'other page newer draft' })
    if (action === 'cancel') fireEvent.click(screen.getByRole('button', { name: A.files.cancel }))
    else fireEvent.change(editor, { target: { value: 'original alpha.txt' } })
    cleanup()
    setup()
    expect(await screen.findByRole('textbox', { name: A.files.editorLabel('alpha.txt') })).toHaveValue('other page newer draft')
  })

  it('后续编辑遭遇存储配额错误，保存文件后仍能清理本页之前成功存过的副本', async () => {
    const page = setup()
    await screen.findByText('original alpha.txt')
    fireEvent.click(screen.getByRole('button', { name: A.files.edit }))
    const editor = screen.getByRole('textbox', { name: A.files.editorLabel('alpha.txt') })
    fireEvent.change(editor, { target: { value: 'earlier persisted draft' } })
    const blocked = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('quota', 'QuotaExceededError') })
    fireEvent.change(editor, { target: { value: 'latest draft saved to file' } })
    expect(screen.getByRole('alert')).toHaveTextContent('草稿仅保存在当前页面')
    fireEvent.click(screen.getByRole('button', { name: A.files.save }))
    await waitFor(() => expect(page.onSaved).toHaveBeenCalledOnce())
    blocked.mockRestore()
    cleanup()
    setup()
    await screen.findByText('original alpha.txt')
    expect(screen.queryByDisplayValue('earlier persisted draft')).not.toBeInTheDocument()
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument()
  })
})

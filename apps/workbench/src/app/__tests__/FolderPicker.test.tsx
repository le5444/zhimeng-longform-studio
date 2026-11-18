/**
 * 选文件夹页（纠偏 P2「项目 = 文件夹」；9-29 回炉重造后不再给文件夹打「书」的标记）：打开已有的、新建空白的。
 * 文件夹由后端列（这里假的），打开成项目交给外面（workspace.openFolder）。
 */
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { FsListing } from '../../api/fs'
import { FolderPicker, folderNameProblem } from '../folder/FolderPicker'
import { FT } from '../folder/folderText'

const api = vi.hoisted(() => ({
  listPlaces: vi.fn(),
  listFolder: vi.fn(),
  makeFolder: vi.fn(),
}))

vi.mock('../../api/fs', () => api)

const LIBRARY = 'C:\\Users\\me\\Documents\\写小说工作区'

function listing(path: string, entries: Array<Partial<FsListing['entries'][number]> & { name: string }> = []): FsListing {
  return {
    schema: 'zhimeng.fs-list.v1',
    path,
    parent: path.includes('\\') ? path.slice(0, path.lastIndexOf('\\')) || null : null,
    breadcrumbs: [{ name: path.split('\\').pop() ?? path, path }],
    is_project: false,
    entries: entries.map((entry) => ({
      path: `${path}\\${entry.name}`,
      is_link: false,
      is_project: false,
      looks_like_book: false,
      ...entry,
    })),
    total: entries.length,
    truncated: false,
  }
}

function inspection(projectId: string, name: string) {
  return { project: { project_id: projectId, display_name: name } } as never
}

function renderPage(mode: 'open' | 'new', openFolder = vi.fn(async () => inspection('p-new', '宗门'))) {
  const onOpened = vi.fn()
  const onCancel = vi.fn()
  render(<FolderPicker mode={mode} openFolder={openFolder} scanProgress={null} onOpened={onOpened} onCancel={onCancel} />)
  return { openFolder, onOpened, onCancel }
}

afterEach(cleanup)

it('初次目录返回时保留已聚焦的空地址，避免粘贴被默认路径插入', async () => {
  let resolve!: (value: FsListing) => void
  api.listFolder.mockReturnValueOnce(new Promise<FsListing>((done) => { resolve = done }))
  renderPage('open')
  await waitFor(() => expect(api.listFolder).toHaveBeenCalled())
  const input = screen.getByRole('textbox', { name: FT.address })
  fireEvent.focus(input)
  await act(async () => { resolve(listing(LIBRARY)) })
  expect(input).toHaveValue('')
  fireEvent.change(input, { target: { value: 'D:\\采购资料' } })
  fireEvent.submit(input.closest('form')!)
  await waitFor(() => expect(api.listFolder).toHaveBeenLastCalledWith('D:\\采购资料'))
})

beforeEach(() => {
  vi.clearAllMocks()
  api.listPlaces.mockResolvedValue({
    places: [
      { label: '主目录', path: 'C:\\Users\\me', kind: 'home' },
      { label: '文档', path: 'C:\\Users\\me\\Documents', kind: 'documents' },
      { label: '写小说工作区', path: LIBRARY, kind: 'trusted' },
      { label: 'C:\\', path: 'C:\\', kind: 'drive' },
    ],
  })
  api.listFolder.mockImplementation(async (path: string) => {
    if (path === LIBRARY) {
      return listing(LIBRARY, [
        { name: '宗门', looks_like_book: true },
        { name: '代码仓库', is_project: true },
      ])
    }
    return listing(path)
  })
})

describe('选文件夹页', () => {
  it('打开：先停在书库这类常用文件夹，列出子文件夹，打开过的标出来', async () => {
    renderPage('open')
    expect(await screen.findByText('宗门')).toBeInTheDocument()
    expect(api.listFolder).toHaveBeenCalledWith(LIBRARY)
    expect(screen.getAllByText(FT.projectMark).length).toBeGreaterThan(0)
    expect(screen.getByText(FT.current(LIBRARY))).toBeInTheDocument()
  })

  it('打开：点文件夹是走进去；「打开这个文件夹」把现在这一层交给外面打开成项目', async () => {
    const { openFolder, onOpened } = renderPage('open')
    fireEvent.click(await screen.findByTitle(FT.enter('宗门')))
    await waitFor(() => expect(api.listFolder).toHaveBeenLastCalledWith(`${LIBRARY}\\宗门`))
    await screen.findByText(FT.current(`${LIBRARY}\\宗门`))
    fireEvent.click(screen.getByRole('button', { name: FT.openThis }))
    await waitFor(() => expect(onOpened).toHaveBeenCalledWith('p-new'))
    expect(openFolder).toHaveBeenCalledWith(`${LIBRARY}\\宗门`)
  })

  it('打开：每一行的「打开」直接把那个子文件夹打开，不用先走进去', async () => {
    const { openFolder, onOpened } = renderPage('open')
    await screen.findByText('代码仓库')
    fireEvent.click(screen.getByTitle(FT.openEntryHint('代码仓库')))
    await waitFor(() => expect(onOpened).toHaveBeenCalled())
    expect(openFolder).toHaveBeenCalledWith(`${LIBRARY}\\代码仓库`)
  })

  it('打开失败：原因照实说，人还在这一页，能换一个再试', async () => {
    const openFolder = vi.fn(async () => {
      throw new Error('project scan exceeds the 20000 file limit')
    })
    const { onOpened } = renderPage('open', openFolder)
    await screen.findByText('宗门')
    fireEvent.click(screen.getByRole('button', { name: FT.openThis }))
    expect(await screen.findByText(/20000 file limit/)).toBeInTheDocument()
    expect(screen.getByText(new RegExp(FT.openFailed))).toBeInTheDocument()
    // 扫描上限：原话之外补一句人话
    expect(screen.getByText(/可以打开里面的某个子文件夹/)).toBeInTheDocument()
    expect(onOpened).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: FT.openThis })).not.toBeDisabled()
  })

  it('地址栏能直接粘贴资源管理器里复制的地址（带引号也行），回车就去', async () => {
    renderPage('open')
    await screen.findByText('宗门')
    const address = screen.getByRole('textbox', { name: FT.address })
    fireEvent.change(address, { target: { value: 'D:\\资料' } })
    fireEvent.submit(address.closest('form')!)
    await waitFor(() => expect(api.listFolder).toHaveBeenLastCalledWith('D:\\资料'))
  })

  it('刚打开就粘地址：常用位置回来得晚，也不跳回默认位置、不盖掉粘的地址', async () => {
    let resolvePlaces!: (value: unknown) => void
    api.listPlaces.mockReturnValueOnce(new Promise((resolve) => { resolvePlaces = resolve }))
    renderPage('open')
    const address = screen.getByRole('textbox', { name: FT.address })
    fireEvent.change(address, { target: { value: 'D:\\资料' } })
    fireEvent.submit(address.closest('form')!)
    await waitFor(() => expect(api.listFolder).toHaveBeenCalledWith('D:\\资料'))
    resolvePlaces({ places: [{ label: '写小说工作区', path: LIBRARY, kind: 'trusted' }] })
    expect(await screen.findByText(FT.current('D:\\资料'))).toBeInTheDocument()
    expect(api.listFolder).not.toHaveBeenCalledWith(LIBRARY)
    expect(address).toHaveValue('D:\\资料')
  })

  it('打开整个盘时提醒一句会慢，但照样能打开', async () => {
    renderPage('open')
    await screen.findByText('宗门')
    fireEvent.click(screen.getByTitle(`${FT.placeKinds.drive}：C:\\`))
    expect(await screen.findByText(FT.bigFolderHint)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: FT.openThis })).not.toBeDisabled()
  })

  it('新建：名字当场查（Windows 不认的字、这里已经有了），没问题就先建再打开', async () => {
    api.makeFolder.mockResolvedValue({ path: `${LIBRARY}\\宗门群像` })
    const { openFolder, onOpened } = renderPage('new', vi.fn(async () => inspection('p-made', '宗门群像')))
    await screen.findByText('宗门')
    const input = screen.getByPlaceholderText(FT.namePlaceholder)
    const create = screen.getByRole('button', { name: FT.createAndOpen })

    fireEvent.change(input, { target: { value: '宗门:群像' } })
    expect(screen.getByText(FT.nameBadChars)).toBeInTheDocument()
    expect(create).toBeDisabled()

    fireEvent.change(input, { target: { value: '宗门' } })
    expect(screen.getByText(FT.nameTaken('宗门'))).toBeInTheDocument()
    expect(create).toBeDisabled()

    fireEvent.change(input, { target: { value: '宗门群像' } })
    expect(screen.getByText(FT.willCreate(`${LIBRARY}\\宗门群像`))).toBeInTheDocument()
    fireEvent.click(create)
    await waitFor(() => expect(onOpened).toHaveBeenCalledWith('p-made'))
    expect(api.makeFolder).toHaveBeenCalledWith(LIBRARY, '宗门群像')
    expect(openFolder).toHaveBeenCalledWith(`${LIBRARY}\\宗门群像`)
  })

  it('取消回到对话', async () => {
    const { onCancel } = renderPage('new')
    await screen.findByText('宗门')
    fireEvent.click(screen.getByRole('button', { name: FT.cancel }))
    expect(onCancel).toHaveBeenCalledOnce()
  })
})

describe('folderNameProblem', () => {
  it('和后端同一套 Windows 规矩', () => {
    expect(folderNameProblem('', [])).toBeNull()
    expect(folderNameProblem('宗门群像', [])).toBeNull()
    expect(folderNameProblem('a/b', [])).toBe(FT.nameBadChars)
    expect(folderNameProblem('结尾是点.', [])).toBe(FT.nameBadEnd)
    expect(folderNameProblem('con', [])).toBe(FT.nameReserved('con'))
    expect(folderNameProblem('NUL.txt', [])).toBe(FT.nameReserved('NUL.txt'))
    expect(folderNameProblem('Books', ['books'])).toBe(FT.nameTaken('Books'))
  })
})

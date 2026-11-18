/**
 * 左栏（9-29 回炉重造）：「新对话」、搜索、「对话」「项目」两段；操作收在「⋯」菜单里，置顶、隐藏、段折叠记在本机。
 */
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProjectConversationGroup, ConversationRow, ConversationsApi } from '../lib/useConversations'
import { Sidebar } from '../Sidebar'
import { A } from '../text'

vi.mock('../../api/sessionSearch', () => ({ searchSessionMessages: vi.fn(async () => ({ matches: [], has_more: false, next_before_message_id: null })) }))

const S = A.sidebar

afterEach(cleanup)
beforeEach(() => window.localStorage.clear())

function chat(id: string, title: string, projectId: string | null = null, pinned = false, empty = false): ConversationRow {
  return { sessionId: id, title, projectId, pinned, updatedAt: '2026-09-15T10:00:00', empty }
}

function makeConversations(over: Partial<ConversationsApi> = {}): ConversationsApi {
  return {
    state: 'ready',
    error: null,
    loose: [],
    projects: [],
    byId: new Map(),
    refresh: vi.fn(),
    rebind: vi.fn(),
    togglePin: vi.fn(async () => undefined),
    rename: vi.fn(async () => undefined),
    remove: vi.fn(async () => undefined),
    ...over,
  }
}

function renderSide(conversations: ConversationsApi, over: Record<string, unknown> = {}) {
  const handlers = {
    onOpenConversation: vi.fn(),
    onNewConversation: vi.fn(),
    onRenameConversation: vi.fn(),
    onDeleteConversation: vi.fn(),
    onOpenProject: vi.fn(),
    onNewFolder: vi.fn(),
    onOpenFolder: vi.fn(),
    onRenameProject: vi.fn(),
    onRevealProject: vi.fn(),
    onHideProject: vi.fn(),
    onOpenSettings: vi.fn(),
    onCollapse: vi.fn(),
    notify: vi.fn(),
    ...over,
  }
  render(<Sidebar conversations={conversations} activeSessionId={null} activeProjectId={null} {...handlers} />)
  return handlers
}

function menuItem(name: string | RegExp) {
  return within(screen.getByRole('menu')).getByRole('menuitem', { name })
}

function projectNames(): string[] {
  const section = screen.getByRole('region', { name: S.projects })
  return Array.from(section.querySelectorAll('.zm-side-row.is-project .zm-side-row-title')).map((node) => node.textContent ?? '')
}

describe('左栏', () => {
  it('后台任务显示进行中，仍可打开其他对话或新建', () => {
    const working = { ...chat('a', '核对表格'), running: true }
    const other = chat('b', '讨论代码')
    const handlers = renderSide(makeConversations({ loose: [working, other] }))
    expect(screen.getByLabelText('任务进行中')).toBeInTheDocument()
    fireEvent.click(screen.getByText('讨论代码'))
    expect(handlers.onOpenConversation).toHaveBeenCalledWith(other)
    fireEvent.click(screen.getByRole('button', { name: S.newChat }))
    expect(handlers.onNewConversation).toHaveBeenCalledWith(null)
  })
  it('最上面「新对话」开一条不打开文件夹的对话；「添加项目」里两项：新建空白文件夹、打开已有文件夹', () => {
    const handlers = renderSide(makeConversations())
    fireEvent.click(screen.getByRole('button', { name: S.newChat }))
    expect(handlers.onNewConversation).toHaveBeenCalledWith(null)

    fireEvent.click(screen.getByRole('button', { name: S.addProject }))
    fireEvent.click(menuItem(S.newFolder))
    expect(handlers.onNewFolder).toHaveBeenCalledOnce()
    // 点完一项菜单就收起
    expect(screen.queryByRole('menu')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: S.addProject }))
    fireEvent.click(menuItem(S.openFolder))
    expect(handlers.onOpenFolder).toHaveBeenCalledOnce()
  })

  it('项目按最近聊过排（置顶的除外），悬停名字看得到文件夹在哪', () => {
    const projects: ProjectConversationGroup[] = [
      { projectId: 'b1', name: '老项目', rootPath: 'D:\\书\\老项目', conversations: [{ ...chat('a1', '旧对话', 'b1'), updatedAt: '2026-09-01T10:00:00' }], latest: null },
      { projectId: 'b2', name: '新项目', rootPath: 'D:\\代码\\新项目', conversations: [{ ...chat('a2', '新对话', 'b2'), updatedAt: '2026-09-27T10:00:00' }], latest: null },
      { projectId: 'b3', name: '没聊过', conversations: [], latest: null },
    ]
    renderSide(makeConversations({ projects }))
    expect(projectNames()).toEqual(['新项目', '老项目', '没聊过'])
    expect(screen.getByText('新项目').closest('button')).toHaveAttribute('title', 'D:\\代码\\新项目')
  })

  it('点项目就打开它、展开它下面的对话；「+」直接在里面开新对话', () => {
    const row = chat('a1', '甲书的对话', 'b1')
    const handlers = renderSide(makeConversations({ projects: [{ projectId: 'b1', name: '甲书', conversations: [row], latest: null }] }))
    expect(screen.queryByText('甲书的对话')).toBeNull()
    fireEvent.click(screen.getByText('甲书'))
    expect(handlers.onOpenProject).toHaveBeenCalledWith('b1')
    fireEvent.click(screen.getByText('甲书的对话'))
    expect(handlers.onOpenConversation).toHaveBeenCalledWith(row)
    fireEvent.click(screen.getByRole('button', { name: S.newChatIn('甲书') }))
    expect(handlers.onNewConversation).toHaveBeenCalledWith('b1')
  })

  it('项目「⋯」：改名只改显示名、在资源管理器里打开，都交给外面', () => {
    const handlers = renderSide(makeConversations({ projects: [{ projectId: 'b1', name: '甲书', rootPath: 'D:\\书\\甲书', conversations: [], latest: null }] }))
    fireEvent.click(screen.getByRole('button', { name: S.projectMore('甲书') }))
    fireEvent.click(menuItem(S.rename))
    const input = screen.getByRole('textbox', { name: S.rename })
    fireEvent.change(input, { target: { value: '甲书第二版' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(handlers.onRenameProject).toHaveBeenCalledWith('b1', '甲书第二版')

    fireEvent.click(screen.getByRole('button', { name: S.projectMore('甲书') }))
    fireEvent.click(menuItem(S.reveal))
    expect(handlers.onRevealProject).toHaveBeenCalledWith('b1')
  })

  it('从列表移除：不再列出、记在本机，「已移除的项目」里能放回来', () => {
    const handlers = renderSide(makeConversations({
      projects: [
        { projectId: 'b1', name: '甲书', conversations: [], latest: null },
        { projectId: 'b2', name: '乙书', conversations: [], latest: null },
      ],
    }))
    fireEvent.click(screen.getByRole('button', { name: S.projectMore('乙书') }))
    fireEvent.click(menuItem(S.hide))
    expect(handlers.onHideProject).toHaveBeenCalledWith('b2')
    expect(projectNames()).toEqual(['甲书'])
    expect(window.localStorage.getItem('zhimeng.desk.nav-hidden-projects')).toContain('b2')

    fireEvent.click(screen.getByText(S.hidden(1)))
    fireEvent.click(screen.getByRole('button', { name: S.restore }))
    expect(projectNames()).toEqual(['甲书', '乙书'])
    expect(window.localStorage.getItem('zhimeng.desk.nav-hidden-projects')).not.toContain('b2')
    expect(screen.queryByText(S.hidden(1))).toBeNull()
  })

  it('项目能置顶：置顶的排最前，本机记住', () => {
    renderSide(makeConversations({
      projects: [
        { projectId: 'b1', name: '甲书', conversations: [chat('a1', '甲书的对话', 'b1')], latest: null },
        { projectId: 'b2', name: '乙书', conversations: [], latest: null },
      ],
    }))
    expect(projectNames()).toEqual(['甲书', '乙书'])
    fireEvent.click(screen.getByRole('button', { name: S.projectMore('乙书') }))
    fireEvent.click(menuItem(S.pin))
    expect(projectNames()).toEqual(['乙书', '甲书'])
    expect(window.localStorage.getItem('zhimeng.desk.nav-pinned-projects')).toContain('b2')
  })

  it('对话多了只露最近 6 条，剩下的折在「显示更多」里', () => {
    renderSide(makeConversations({ loose: Array.from({ length: 9 }, (_, i) => chat(`s${i}`, `第 ${i + 1} 条闲聊`)) }))
    expect(screen.queryByText('第 7 条闲聊')).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: S.showMore(3) }))
    expect(screen.getByText('第 7 条闲聊')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: S.showLess }))
    expect(screen.queryByText('第 7 条闲聊')).toBeNull()
  })

  it('项目多了也只露前 6 个，正开着的那个不管排第几都露出来', () => {
    const projects: ProjectConversationGroup[] = Array.from({ length: 9 }, (_, i) => ({
      projectId: `b${i}`,
      name: `第 ${i + 1} 个项目`,
      conversations: [{ ...chat(`c${i}`, '一条对话', `b${i}`), updatedAt: `2026-09-${String(20 - i).padStart(2, '0')}T10:00:00` }],
      latest: null,
    }))
    renderSide(makeConversations({ projects }), { activeProjectId: 'b8' })
    expect(screen.queryByText('第 7 个项目')).toBeNull()
    expect(screen.getByText('第 9 个项目')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: S.showMore(2) }))
    expect(screen.getByText('第 7 个项目')).toBeTruthy()
  })

  it('两段都能整段折起来，折法记在本机', () => {
    renderSide(makeConversations({ loose: [chat('s1', '一条闲聊')] }))
    const toggle = within(screen.getByRole('region', { name: S.chats })).getByRole('button', { name: S.chats })
    fireEvent.click(toggle)
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    expect(screen.queryByText('一条闲聊')).toBeNull()
    expect(window.localStorage.getItem('zhimeng.app.sidebar-sections')).toContain('chats')
  })

  it('对话「⋯」：置顶、改名、删除', async () => {
    const row = chat('s1', '一条闲聊')
    const conversations = makeConversations({ loose: [row], byId: new Map([[row.sessionId, row]]) })
    const handlers = renderSide(conversations)

    fireEvent.click(screen.getByRole('button', { name: S.chatMore('一条闲聊') }))
    fireEvent.click(menuItem(S.pin))
    await waitFor(() => expect(conversations.togglePin).toHaveBeenCalledWith('s1', true))

    fireEvent.click(screen.getByRole('button', { name: S.chatMore('一条闲聊') }))
    fireEvent.click(menuItem(S.rename))
    const input = screen.getByRole('textbox', { name: S.rename })
    fireEvent.change(input, { target: { value: '改过的名字' } })
    fireEvent.keyDown(input, { key: 'Enter' })
    expect(handlers.onRenameConversation).toHaveBeenCalledWith(row, '改过的名字')
    expect(screen.queryByRole('textbox', { name: S.rename })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: S.chatMore('一条闲聊') }))
    fireEvent.click(menuItem(A.common.delete))
    expect(handlers.onDeleteConversation).toHaveBeenCalledWith(row)
  })

  it('一句话都没说过的空对话不列出来，正开着的那条除外；全是空的就说还没有对话', () => {
    const { unmount } = render(
      <Sidebar
        {...renderProps()}
        conversations={makeConversations({ loose: [chat('e1', '空的一', null, false, true), chat('e2', '空的二', null, false, true), chat('r1', '聊过内容的对话')] })}
        activeSessionId="e2"
      />,
    )
    expect(screen.queryByText('空的一')).toBeNull()
    expect(screen.getByText('空的二')).toBeTruthy()
    expect(screen.getByText('聊过内容的对话')).toBeTruthy()
    unmount()

    renderSide(makeConversations({ loose: [chat('e1', '空的一', null, false, true)] }))
    expect(screen.getByText(S.chatsEmpty)).toBeTruthy()
  })

  it('搜索按名字找，项目里的对话也找，旁边写着在哪个项目；找不到就照实说', () => {
    renderSide(makeConversations({
      loose: [chat('s1', '周报怎么写')],
      projects: [{ projectId: 'b1', name: '甲书', conversations: [chat('a1', '第三章周报', 'b1'), chat('a2', '别的', 'b1')], latest: null }],
    }))
    fireEvent.change(screen.getByRole('searchbox', { name: S.search }), { target: { value: '周报' } })
    expect(screen.getByText('周报怎么写')).toBeTruthy()
    expect(screen.getByText('第三章周报').closest('button')?.textContent).toContain('甲书')
    expect(screen.queryByText('别的')).toBeNull()
    fireEvent.change(screen.getByRole('searchbox', { name: S.search }), { target: { value: '没有这个' } })
    expect(screen.getByText(S.searchEmpty)).toBeTruthy()
  })

  it('搜索范围同时筛标题，Escape 返回原来的导航而不打开其他对话', () => {
    const handlers = renderSide(makeConversations({ loose: [chat('s1', '采购预算')], projects: [
      { projectId: 'p1', name: '采购项目', conversations: [chat('s2', '采购清单', 'p1')], latest: null },
    ] }), { activeProjectId: 'p1' })
    const input = screen.getByRole('searchbox', { name: S.search })
    fireEvent.change(input, { target: { value: '采购' } })
    fireEvent.change(screen.getByRole('combobox', { name: '搜索范围' }), { target: { value: 'project' } })
    expect(screen.queryByText('采购预算')).toBeNull()
    expect(screen.getByText('采购清单')).toBeVisible()
    fireEvent.keyDown(input, { key: 'Escape' })
    expect(input).toHaveValue('')
    expect(screen.queryByLabelText('正文搜索结果')).toBeNull()
    expect(screen.getByText('采购预算')).toBeVisible()
    expect(handlers.onOpenConversation).not.toHaveBeenCalled()
  })

  it('底下「设置」和收起交给外面', () => {
    const handlers = renderSide(makeConversations())
    fireEvent.click(screen.getByRole('button', { name: S.settings }))
    expect(handlers.onOpenSettings).toHaveBeenCalledOnce()
    fireEvent.click(screen.getByRole('button', { name: S.collapse }))
    expect(handlers.onCollapse).toHaveBeenCalledOnce()
  })
})

function renderProps() {
  return {
    conversations: makeConversations(),
    activeSessionId: null,
    activeProjectId: null,
    onOpenConversation: vi.fn(),
    onNewConversation: vi.fn(),
    onRenameConversation: vi.fn(),
    onDeleteConversation: vi.fn(),
    onOpenProject: vi.fn(),
    onNewFolder: vi.fn(),
    onOpenFolder: vi.fn(),
    onRenameProject: vi.fn(),
    onRevealProject: vi.fn(),
    onHideProject: vi.fn(),
    onOpenSettings: vi.fn(),
    onCollapse: vi.fn(),
    notify: vi.fn(),
  }
}

import { useEffect, useMemo, useState, type ReactNode } from 'react'
import './sidebar.css'
import {
  ChevronDown,
  ChevronRight,
  Folder,
  FolderOpen,
  FolderPlus,
  MoreHorizontal,
  PanelLeftClose,
  PanelsTopLeft,
  Pencil,
  Pin,
  PinOff,
  Plus,
  Search,
  Settings,
  SquarePen,
  Trash2,
  EyeOff,
  ExternalLink,
} from 'lucide-react'
import type { ProjectConversationGroup, ConversationRow, ConversationsApi } from './lib/useConversations'
import { authorFacingError } from '../lib/authorFacingError'
import { readIdSet, toggled, writeIdSet } from './lib/local'
import { shortDay } from './lib/time'
import { A } from './text'
import { IconBtn, Menu, Spinner, type Notify } from './ui'
import { ConversationSearch } from './ConversationSearch'

const S = A.sidebar

/** 一段里直接露出几条，多的折在「显示更多」后面。 */
const PREVIEW = 6
const SECTIONS_KEY = 'zhimeng.app.sidebar-sections'
const PINNED_PROJECTS_KEY = 'zhimeng.desk.nav-pinned-projects'
const HIDDEN_PROJECTS_KEY = 'zhimeng.desk.nav-hidden-projects'
const OPEN_PROJECTS_KEY = 'zhimeng.app.sidebar-open-projects'

export interface SidebarProps {
  conversations: ConversationsApi
  activeSessionId: string | null
  activeProjectId: string | null
  onOpenConversation: (row: ConversationRow) => void
  /** 新开一条对话；projectId 为空就是不打开文件夹的对话。 */
  onNewConversation: (projectId: string | null) => void
  onRenameConversation: (row: ConversationRow, title: string) => void
  onDeleteConversation: (row: ConversationRow) => void
  onOpenProject: (projectId: string) => void
  onNewFolder: () => void
  onOpenFolder: () => void
  onRenameProject: (projectId: string, name: string) => void
  onRevealProject: (projectId: string) => void
  /** 从列表移除正开着的那个项目时，外面先换到不打开文件夹的对话。 */
  onHideProject: (projectId: string) => void
  onOpenSettings: () => void
  onCollapse: () => void
  notify: Notify
}

/**
 * 左栏（9-29 重做）：最上面「新对话」和搜索，下面两段——「对话」（没打开文件夹的）和「项目」（文件夹，
 * 展开是它里面的对话），最底下「设置」。照 Codex / Claude 桌面版的样子：平时安静，悬停才露操作，
 * 操作收进「⋯」菜单；置顶的排最前、别的按最近聊过排；空对话（一句话都没说过）不列，正开着的除外。
 */
export function Sidebar(props: SidebarProps) {
  const { conversations, activeSessionId, activeProjectId, notify } = props
  const [query, setQuery] = useState('')
  const [searchScope, setSearchScope] = useState<'all' | 'project'>('all')
  const [sections, setSections] = useState(() => readIdSet(SECTIONS_KEY))
  const [openProjects, setOpenProjects] = useState(() => readIdSet(OPEN_PROJECTS_KEY))
  const [pinnedProjects, setPinnedProjects] = useState(() => readIdSet(PINNED_PROJECTS_KEY))
  const [hiddenProjects, setHiddenProjects] = useState(() => readIdSet(HIDDEN_PROJECTS_KEY))
  const [expanded, setExpanded] = useState<ReadonlySet<string>>(() => new Set())
  const [renaming, setRenaming] = useState<{ kind: 'chat' | 'project'; id: string; text: string } | null>(null)

  // 正开着的项目自动展开。
  useEffect(() => {
    if (!activeProjectId) return
    setOpenProjects((current) => {
      if (current.has(activeProjectId)) return current
      const next = toggled(current, activeProjectId, true)
      writeIdSet(OPEN_PROJECTS_KEY, next)
      return next
    })
  }, [activeProjectId])

  const collapsedSection = (key: string) => sections.has(key)
  const toggleSection = (key: string) => setSections((current) => {
    const next = toggled(current, key)
    writeIdSet(SECTIONS_KEY, next)
    return next
  })
  const toggleProjectOpen = (id: string, on?: boolean) => setOpenProjects((current) => {
    const next = toggled(current, id, on)
    writeIdSet(OPEN_PROJECTS_KEY, next)
    return next
  })
  const togglePinProject = (id: string) => setPinnedProjects((current) => {
    const next = toggled(current, id)
    writeIdSet(PINNED_PROJECTS_KEY, next)
    return next
  })
  const setHidden = (id: string, hidden: boolean) => {
    setHiddenProjects((current) => {
      const next = toggled(current, id, hidden)
      writeIdSet(HIDDEN_PROJECTS_KEY, next)
      return next
    })
    if (hidden) props.onHideProject(id)
  }
  const togglePinChat = (row: ConversationRow) => {
    void conversations.togglePin(row.sessionId, !row.pinned).catch((error: unknown) => notify('warn', authorFacingError(error, S.pinFailed)))
  }
  const commitRename = () => {
    if (!renaming) return
    const text = renaming.text.trim()
    if (renaming.kind === 'chat') {
      const row = conversations.byId.get(renaming.id)
      if (row && text && text !== row.title) props.onRenameConversation(row, text)
    } else {
      const project = conversations.projects.find((item) => item.projectId === renaming.id)
      if (project && text && text !== project.name) props.onRenameProject(renaming.id, text)
    }
    setRenaming(null)
  }

  const visibleRow = (row: ConversationRow) => !row.empty || row.sessionId === activeSessionId
  const projects = useMemo(() => {
    const shown = conversations.projects.filter((book) => !hiddenProjects.has(book.projectId) || book.projectId === activeProjectId)
    return shown.sort((left, right) => {
      const pinned = Number(pinnedProjects.has(right.projectId)) - Number(pinnedProjects.has(left.projectId))
      if (pinned !== 0) return pinned
      const recency = (right.conversations[0]?.updatedAt ?? '').localeCompare(left.conversations[0]?.updatedAt ?? '')
      return recency !== 0 ? recency : left.name.localeCompare(right.name, 'zh-CN')
    })
  }, [activeProjectId, conversations.projects, hiddenProjects, pinnedProjects])
  const removed = conversations.projects.filter((book) => hiddenProjects.has(book.projectId) && book.projectId !== activeProjectId)
  // 折着的时候只露前几个，正开着的那个不管排第几都露出来。
  const shownProjects = expanded.has('projects') ? projects : projects.filter((project, index) => index < PREVIEW || project.projectId === activeProjectId)

  const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
  const searchHits = useMemo(() => {
    if (words.length === 0) return null
    const all = [...conversations.loose, ...conversations.projects.flatMap((book) => book.conversations)]
    return all.filter((row) => visibleRow(row) && (searchScope === 'all' || row.projectId === activeProjectId) && words.every((word) => (row.title || S.untitled).toLowerCase().includes(word)))
  }, [conversations.projects, conversations.loose, words.join(' '), searchScope, activeProjectId, activeSessionId]) // eslint-disable-line react-hooks/exhaustive-deps

  const projectName = (id: string | null) => (id ? conversations.projects.find((book) => book.projectId === id)?.name ?? null : null)

  const chatList = (key: string, rows: ConversationRow[], nested = false) => {
    const list = rows.filter(visibleRow)
    const all = expanded.has(key)
    const shown = all ? list : list.slice(0, PREVIEW)
    return (
      <>
        <ul className="zm-side-list" role="list">
          {shown.map((row) => (
            <li key={row.sessionId}>
              <ChatItem
                row={row}
                nested={nested}
                active={row.sessionId === activeSessionId}
                renaming={renaming?.kind === 'chat' && renaming.id === row.sessionId ? renaming.text : null}
                onRenameText={(text) => setRenaming({ kind: 'chat', id: row.sessionId, text })}
                onCommitRename={commitRename}
                onCancelRename={() => setRenaming(null)}
                onOpen={() => props.onOpenConversation(row)}
                onStartRename={() => setRenaming({ kind: 'chat', id: row.sessionId, text: row.title })}
                onTogglePin={() => togglePinChat(row)}
                onDelete={() => props.onDeleteConversation(row)}
              />
            </li>
          ))}
        </ul>
        {list.length > PREVIEW ? (
          <button type="button" className={`zm-side-more${nested ? ' is-nested' : ''}`} onClick={() => setExpanded((current) => toggled(current, key))}>
            {all ? S.showLess : S.showMore(list.length - PREVIEW)}
          </button>
        ) : null}
      </>
    )
  }

  return (
    <nav className="zm-side" aria-label={S.label}>
      <div className="zm-side-brand">
        <PanelsTopLeft aria-hidden />
        <div><strong>{S.brand}</strong><small>{S.workspace}</small></div>
      </div>
      <div className="zm-side-top">
        <button type="button" className="zm-side-new" onClick={() => props.onNewConversation(null)} title={S.newChatHint}>
          <SquarePen aria-hidden />
          <span>{S.newChat}</span>
        </button>
        <label className="zm-side-search">
          <Search aria-hidden />
          <input type="search" value={query} placeholder="搜索标题和正文" aria-label={S.search} maxLength={500} onChange={(event) => setQuery(event.target.value)} onKeyDown={event => { if (event.key === 'Escape') { event.preventDefault(); setQuery('') } }} />
        </label>
        {query.trim() ? <label className="zm-side-search-scope">范围<select aria-label="搜索范围" value={searchScope} onChange={event => setSearchScope(event.target.value as 'all' | 'project')}>
          <option value="all">全部对话</option><option value="project">{activeProjectId ? '当前项目' : '自由对话'}</option>
        </select></label> : null}
      </div>

      <div className="zm-side-scroll">
        {conversations.state === 'loading' && conversations.loose.length === 0 && conversations.projects.length === 0 ? (
          <p className="zm-side-note">{S.loading}</p>
        ) : null}
        {conversations.state === 'error' ? (
          <p className="zm-side-note">
            {S.loadFailed} <button type="button" className="zm-link" onClick={conversations.refresh}>{A.common.retry}</button>
          </p>
        ) : null}

        {searchHits ? (
          <section className="zm-side-section">
            <h3 className="zm-side-search-title">标题</h3>
            {searchHits.length === 0 ? <p className="zm-side-note">{S.searchEmpty}</p> : null}
            <ul className="zm-side-list" role="list">
              {searchHits.map((row) => (
                <li key={row.sessionId}>
                  <ChatItem
                    row={row}
                    context={projectName(row.projectId)}
                    active={row.sessionId === activeSessionId}
                    renaming={null}
                    onRenameText={() => undefined}
                    onCommitRename={() => undefined}
                    onCancelRename={() => undefined}
                    onOpen={() => props.onOpenConversation(row)}
                    onStartRename={() => { setQuery(''); setRenaming({ kind: 'chat', id: row.sessionId, text: row.title }) }}
                    onTogglePin={() => togglePinChat(row)}
                    onDelete={() => props.onDeleteConversation(row)}
                  />
                </li>
              ))}
            </ul>
            <ConversationSearch query={query.trim()} scope={searchScope} projectId={activeProjectId} />
          </section>
        ) : (
          <>
            <section className="zm-side-section" aria-label={S.chats}>
              <SectionHead title={S.chats} collapsed={collapsedSection('chats')} onToggle={() => toggleSection('chats')} />
              {collapsedSection('chats') ? null : (
                conversations.loose.some(visibleRow) ? chatList('chats', conversations.loose) : <p className="zm-side-note">{S.chatsEmpty}</p>
              )}
            </section>

            <section className="zm-side-section" aria-label={S.projects}>
              <SectionHead
                title={S.projects}
                collapsed={collapsedSection('projects')}
                onToggle={() => toggleSection('projects')}
                action={(
                  <Menu
                    label={S.addProject}
                    align="end"
                    items={[
                      { key: 'new', label: S.newFolder, hint: S.newFolderHint, icon: <FolderPlus />, onSelect: props.onNewFolder },
                      { key: 'open', label: S.openFolder, hint: S.openFolderHint, icon: <FolderOpen />, onSelect: props.onOpenFolder },
                    ]}
                    trigger={({ open, toggle, ref, menuId }) => (
                      <IconBtn ref={ref} size="sm" label={S.addProject} icon={<Plus />} aria-expanded={open} aria-controls={open ? menuId : undefined} onClick={toggle} />
                    )}
                  />
                )}
              />
              {collapsedSection('projects') ? null : (
                <>
                  {projects.length === 0 ? <p className="zm-side-note">{S.projectsEmpty}</p> : null}
                  <ul className="zm-side-list" role="list">
                    {shownProjects.map((project) => (
                      <li key={project.projectId}>
                        <ProjectItem
                          project={project}
                          open={openProjects.has(project.projectId)}
                          current={project.projectId === activeProjectId}
                          pinned={pinnedProjects.has(project.projectId)}
                          renaming={renaming?.kind === 'project' && renaming.id === project.projectId ? renaming.text : null}
                          onRenameText={(text) => setRenaming({ kind: 'project', id: project.projectId, text })}
                          onCommitRename={commitRename}
                          onCancelRename={() => setRenaming(null)}
                          onOpen={() => {
                            props.onOpenProject(project.projectId)
                            toggleProjectOpen(project.projectId, project.projectId === activeProjectId ? undefined : true)
                          }}
                          onNewChat={() => props.onNewConversation(project.projectId)}
                          onTogglePin={() => togglePinProject(project.projectId)}
                          onStartRename={() => setRenaming({ kind: 'project', id: project.projectId, text: project.name })}
                          onReveal={() => props.onRevealProject(project.projectId)}
                          onHide={() => setHidden(project.projectId, true)}
                        >
                          {chatList(project.projectId, project.conversations, true)}
                        </ProjectItem>
                      </li>
                    ))}
                  </ul>
                  {expanded.has('projects') || shownProjects.length < projects.length ? (
                    <button type="button" className="zm-side-more" onClick={() => setExpanded((current) => toggled(current, 'projects'))}>
                      {expanded.has('projects') ? S.showLess : S.showMore(projects.length - shownProjects.length)}
                    </button>
                  ) : null}
                  {removed.length > 0 ? (
                    <details className="zm-side-removed">
                      <summary>{S.hidden(removed.length)}</summary>
                      <ul className="zm-side-list" role="list">
                        {removed.map((project) => (
                          <li key={project.projectId} className="zm-side-removed-row">
                            <Folder aria-hidden />
                            <span title={project.rootPath}>{project.name}</span>
                            <button type="button" className="zm-link" onClick={() => setHidden(project.projectId, false)}>{S.restore}</button>
                          </li>
                        ))}
                      </ul>
                    </details>
                  ) : null}
                </>
              )}
            </section>
          </>
        )}
      </div>

      <div className="zm-side-foot">
        <button type="button" className="zm-side-foot-btn" onClick={props.onOpenSettings}>
          <Settings aria-hidden />
          <span>{S.settings}</span>
        </button>
        <IconBtn size="sm" label={S.collapse} icon={<PanelLeftClose />} onClick={props.onCollapse} />
      </div>
    </nav>
  )
}

function SectionHead({ title, collapsed, onToggle, action }: { title: string; collapsed: boolean; onToggle: () => void; action?: ReactNode }) {
  return (
    <div className="zm-side-head">
      <button type="button" className="zm-side-head-toggle" aria-expanded={!collapsed} onClick={onToggle}>
        <span>{title}</span>
        {collapsed ? <ChevronRight aria-hidden /> : <ChevronDown aria-hidden />}
      </button>
      {action}
    </div>
  )
}

function RenameField({ value, placeholder, title, onChange, onCommit, onCancel }: {
  value: string
  placeholder: string
  title?: string
  onChange: (text: string) => void
  onCommit: () => void
  onCancel: () => void
}) {
  return (
    <input
      className="zm-side-rename"
      value={value}
      placeholder={placeholder}
      title={title}
      aria-label={placeholder}
      autoFocus
      onFocus={(event) => event.currentTarget.select()}
      onChange={(event) => onChange(event.target.value)}
      onKeyDown={(event) => {
        if (event.key === 'Enter') onCommit()
        if (event.key === 'Escape') onCancel()
      }}
      onBlur={onCommit}
    />
  )
}

function ChatItem({ row, active, nested = false, context, renaming, onRenameText, onCommitRename, onCancelRename, onOpen, onStartRename, onTogglePin, onDelete }: {
  row: ConversationRow
  active: boolean
  nested?: boolean
  /** 搜索结果里：这条对话在哪个项目里。 */
  context?: string | null
  renaming: string | null
  onRenameText: (text: string) => void
  onCommitRename: () => void
  onCancelRename: () => void
  onOpen: () => void
  onStartRename: () => void
  onTogglePin: () => void
  onDelete: () => void
}) {
  const title = row.title || S.untitled
  if (renaming !== null) {
    return (
      <div className={`zm-side-row is-renaming${nested ? ' is-nested' : ''}`}>
        <RenameField value={renaming} placeholder={S.rename} onChange={onRenameText} onCommit={onCommitRename} onCancel={onCancelRename} />
      </div>
    )
  }
  return (
    <div className={`zm-side-row${active ? ' is-active' : ''}${nested ? ' is-nested' : ''}`}>
      <button type="button" className="zm-side-row-main" aria-current={active ? 'page' : undefined} onClick={onOpen} title={title}>
        {row.pinned ? <Pin className="zm-side-pin" aria-label={S.pin} /> : null}
        <span className="zm-side-row-title">{title}</span>
        {context ? <span className="zm-side-row-context">{context}</span> : null}
        {row.running ? <span className="zm-side-row-status" title="任务进行中"><Spinner label="任务进行中" /></span>
          : <span className="zm-side-row-time">{shortDay(row.updatedAt)}</span>}
      </button>
      <Menu
        label={S.chatMore(title)}
        align="end"
        items={[
          { key: 'pin', label: row.pinned ? S.unpin : S.pin, icon: row.pinned ? <PinOff /> : <Pin />, onSelect: onTogglePin },
          { key: 'rename', label: S.rename, icon: <Pencil />, onSelect: onStartRename },
          { key: 'delete', label: A.common.delete, icon: <Trash2 />, danger: true, onSelect: onDelete },
        ]}
        trigger={({ open, toggle, ref, menuId }) => (
          <IconBtn
            ref={ref}
            size="sm"
            className={`zm-side-row-more${open ? ' is-open' : ''}`}
            label={S.chatMore(title)}
            icon={<MoreHorizontal />}
            aria-expanded={open}
            aria-controls={open ? menuId : undefined}
            onClick={toggle}
          />
        )}
      />
    </div>
  )
}

function ProjectItem({ project, open, current, pinned, renaming, onRenameText, onCommitRename, onCancelRename, onOpen, onNewChat, onTogglePin, onStartRename, onReveal, onHide, children }: {
  project: ProjectConversationGroup
  open: boolean
  current: boolean
  pinned: boolean
  renaming: string | null
  onRenameText: (text: string) => void
  onCommitRename: () => void
  onCancelRename: () => void
  onOpen: () => void
  onNewChat: () => void
  onTogglePin: () => void
  onStartRename: () => void
  onReveal: () => void
  onHide: () => void
  children: ReactNode
}) {
  return (
    <div className={`zm-side-project${current ? ' is-current' : ''}`}>
      {renaming !== null ? (
        <div className="zm-side-row is-renaming">
          <RenameField value={renaming} placeholder={S.rename} title={S.renameProjectHint} onChange={onRenameText} onCommit={onCommitRename} onCancel={onCancelRename} />
        </div>
      ) : (
        <div className={`zm-side-row is-project${current ? ' is-current' : ''}`}>
          <button type="button" className="zm-side-row-main" aria-expanded={open} onClick={onOpen} title={project.rootPath ?? project.name}>
            {open ? <FolderOpen className="zm-side-folder" aria-hidden /> : <Folder className="zm-side-folder" aria-hidden />}
            <span className="zm-side-row-title">{project.name}</span>
            {pinned ? <Pin className="zm-side-pin" aria-label={S.pin} /> : null}
          </button>
          <IconBtn size="sm" className="zm-side-row-more" label={S.newChatIn(project.name)} icon={<Plus />} onClick={onNewChat} />
          <Menu
            label={S.projectMore(project.name)}
            align="end"
            items={[
              { key: 'pin', label: pinned ? S.unpin : S.pin, icon: pinned ? <PinOff /> : <Pin />, onSelect: onTogglePin },
              { key: 'rename', label: S.rename, hint: S.renameProjectHint, icon: <Pencil />, onSelect: onStartRename },
              { key: 'reveal', label: S.reveal, hint: project.rootPath, icon: <ExternalLink />, onSelect: onReveal },
              { key: 'hide', label: S.hide, hint: S.hideHint, icon: <EyeOff />, onSelect: onHide },
            ]}
            trigger={({ open: menuOpen, toggle, ref, menuId }) => (
              <IconBtn
                ref={ref}
                size="sm"
                className={`zm-side-row-more${menuOpen ? ' is-open' : ''}`}
                label={S.projectMore(project.name)}
                icon={<MoreHorizontal />}
                aria-expanded={menuOpen}
                aria-controls={menuOpen ? menuId : undefined}
                onClick={toggle}
              />
            )}
          />
        </div>
      )}
      {open ? <div className="zm-side-project-body">{children}</div> : null}
    </div>
  )
}

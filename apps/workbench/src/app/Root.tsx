import { lazy, Suspense, useCallback, useEffect, useRef, useState, type CSSProperties, type MouseEvent } from 'react'
import { PanelLeftOpen, Search, Settings, SquarePen } from 'lucide-react'
import './zm.css'
import './chat/chat.css'
import './panel/panel.css'
import { revealProject } from '../api/projects'
import { sharedBrowser } from '../api/sharedBrowser'
import { AgentBusyError, useAgentConversation } from '../hooks/useAgentConversation'
import { useChangeSets } from '../hooks/useChangeSets'
import { useProjectWorkspace } from '../hooks/useProjectWorkspace'
import { useRuntimeStatus } from '../hooks/useRuntimeStatus'
import { useSessionFileChanges } from '../hooks/useSessionFileChanges'
import { authorFacingError } from '../lib/authorFacingError'
import { useConversations, type ConversationRow } from './lib/useConversations'
import { useProviderSettings } from './lib/useProviderSettings'
import { T } from './strings'
import { ChatView, type ChatComposerHandle } from './chat/ChatView'
import { readFlag, writeFlag } from './lib/local'
import { useTheme } from './lib/theme'
import { onUiSizeChange, readUiSize, uiSizePixels } from './lib/uiSize'
import { WorkspacePanel, type PanelTab } from './panel/WorkspacePanel'
import type { LocalFileTarget } from './panel/LocalFile'
import { localFileCandidates, resolveFileNavigation } from './lib/fileNavigation'
import type { FileLocation } from './lib/fileReferences'
import type { SettingsSection } from './settings/Settings'
import { SETTINGS_PAGES } from './settings/registry'
import { Sidebar } from './Sidebar'
import { WorkbenchLayout } from './WorkbenchLayout'
import { A } from './text'
import { ConfirmDialog, IconBtn, Toasts, useToasts, type AskConfirm, type ConfirmRequest } from './ui'

type Overlay = { kind: 'settings'; section: SettingsSection } | { kind: 'folder'; mode: 'open' | 'new'; after: 'new-chat' | 'move-chat' } | null
type Pane = 'nav' | 'chat' | 'panel'

const SIDE_KEY = 'zhimeng.app.side-collapsed'
const SettingsView = lazy(() => import('./settings/Settings').then((module) => ({ default: module.SettingsView })))
const FolderPicker = lazy(() => import('./folder/FolderPicker').then((module) => ({ default: module.FolderPicker })))

/**
 * 织梦的根（9-29 回炉重造第二版：界面整套重写，只留数据层——接口和 useAgentConversation 这些 hook）。
 * 左栏对话和项目 | 中间对话（或设置、选文件夹）| 右栏文件和修改（默认收着，点了文件、修改才开）。
 * 布局和数值照前端吸收报告（docs/research/吸收-20260929/前端吸收-GUI.md）A、E 两节。
 */
export default function Root() {
  const composerRef = useRef<ChatComposerHandle | null>(null)
  const { runtime } = useRuntimeStatus()
  const workspace = useProjectWorkspace(runtime.ready)
  const projectId = workspace.selectedProjectId
  const snapshot = workspace.snapshot
  const [preferredSessionId, setPreferredSessionId] = useState<string | null>(null)
  // 右栏开没开、在哪一页、开着哪个文件。发消息时照实告诉 Agent 开着哪个文件——和对话顶上「开着：…」说的是同一个，
  // 右栏收着或在「修改」页就算没开着。
  const [panelOpen, setPanelOpen] = useState(false)
  const [panelTab, setPanelTab] = useState<PanelTab>('files')
  const [openPath, setOpenPath] = useState<string | null>(null)
  const [localFile, setLocalFile] = useState<LocalFileTarget | null>(null)
  const [fileFocus, setFileFocus] = useState<{ projectId: string | null; path: string; location?: FileLocation; request: number } | null>(null)
  const [overlay, setOverlayState] = useState<Overlay>(null)
  const visibleOpenPath = panelOpen && panelTab === 'files' && projectId && snapshot && overlay === null ? openPath : null
  const conversation = useAgentConversation({
    enabled: runtime.ready,
    projectId,
    projectRevision: snapshot?.project.revision ?? null,
    projectFingerprint: snapshot?.project.fingerprint ?? null,
    openFilePath: visibleOpenPath,
    preferredSessionId,
  })
  const review = useChangeSets(projectId, workspace.refresh)
  const provider = useProviderSettings(runtime.ready)
  const { choice: themeChoice, theme, setChoice: setThemeChoice } = useTheme()
  const conversations = useConversations(runtime.ready, workspace.bindings)
  const { toasts, notify, dismiss } = useToasts()
  const [uiSize, setUiSize] = useState(readUiSize)
  useEffect(() => onUiSizeChange(setUiSize), [])

  const [confirm, setConfirm] = useState<ConfirmRequest | null>(null)
  const [confirmBusy, setConfirmBusy] = useState(false)
  const askConfirm = useCallback<AskConfirm>((request) => setConfirm(request), [])
  const runConfirm = async () => {
    if (!confirm) return
    setConfirmBusy(true)
    try {
      await confirm.onConfirm()
      setConfirm(null)
    } catch (error) {
      notify('danger', authorFacingError(error, A.common.unknownError))
    } finally {
      setConfirmBusy(false)
    }
  }

  // ---- 三栏 ----
  const [pane, setPane] = useState<Pane>('chat')
  const setOverlay = useCallback((next: Overlay) => {
    setOverlayState(next)
    setPane('chat')
  }, [])
  const [sideCollapsed, setSideCollapsedState] = useState(() => readFlag(SIDE_KEY, false))
  const setSideCollapsed = (value: boolean) => {
    setSideCollapsedState(value)
    writeFlag(SIDE_KEY, value)
  }
  const [panelWide, setPanelWide] = useState(false)
  const [changeTarget, setChangeTarget] = useState<{ id: string; sessionId: string | null; projectId: string | null } | null>(null)
  const changesFocus = changeTarget?.sessionId === (conversation.session?.session_id ?? null) && changeTarget?.projectId === projectId ? changeTarget.id : null
  const [changesFocusRequest, setChangesFocusRequest] = useState(0)
  const [commandFocus, setCommandFocus] = useState<{ sessionId: string | null; id: string | null; request: number }>({ sessionId: null, id: null, request: 0 })
  const visibleLocalFile = localFile?.sessionId === (conversation.session?.session_id ?? null) && localFile?.projectId === projectId ? localFile : null
  const effectivePanelTab = panelTab === 'commands' || panelTab === 'browser' ? panelTab : projectId && snapshot || visibleLocalFile ? panelTab : 'changes'
  const panelShown = Boolean(panelOpen && (projectId || conversation.session) && overlay === null)
  const returnToConversation = () => {
    closePanel()
    window.requestAnimationFrame(() => document.querySelector<HTMLTextAreaElement>('.zm-composer-input')?.focus({ preventScroll: true }))
  }

  const showPanel = useCallback((tab: PanelTab) => {
    setPanelTab(tab)
    setPanelOpen(true)
    setOverlayState(null)
    setPane('panel')
  }, [])
  const closePanel = () => {
    setPanelOpen(false)
    setPanelWide(false)
    setPane('chat')
  }
  useEffect(() => {
    const bridge = sharedBrowser()
    if (!bridge) return
    let known: Set<string> | null = null, alive = true
    void bridge.state().then(state => { if (alive && !known) known = new Set(state.tabs.map(tab => tab.id)) }).catch(() => {})
    const off = bridge.onChange(state => {
      const previous = known
      known = new Set(state.tabs.map(tab => tab.id))
      if (previous && overlay === null && state.tabs.some(tab => !previous.has(tab.id) && tab.session_id === conversation.session?.session_id)) showPanel('browser')
    })
    return () => { alive = false; off() }
  }, [conversation.session?.session_id, showPanel, overlay])
  const openFile = useCallback((relativePath: string, location?: FileLocation) => {
    setLocalFile(null)
    setOpenPath(relativePath)
    setFileFocus((previous) => ({ projectId, path: relativePath, location, request: (previous?.request ?? 0) + 1 }))
    showPanel('files')
  }, [projectId, showPanel])
  const openLocalFile = useCallback((path: string) => {
    const target = resolveFileNavigation(snapshot, path)
    if (target && projectId) { openFile(target.relativePath, target.location); return }
    if (!localFileCandidates(path).length) { notify('warn', `请使用文件的完整路径打开：${path}`); return }
    setOpenPath(null)
    setLocalFile(previous => ({ path, sessionId: conversation.session?.session_id ?? null, projectId, request: (previous?.request ?? 0) + 1, returnPath: openPath }))
    showPanel('files')
  }, [snapshot, projectId, openFile, notify, conversation.session?.session_id, openPath, showPanel])
  const backFromLocalFile = useCallback(() => {
    const returnPath = visibleLocalFile?.returnPath ?? null
    setLocalFile(null)
    if (!projectId || !snapshot) { closePanel(); return }
    setOpenPath(returnPath)
    setPanelTab('files')
    setPanelOpen(true)
    setPane('panel')
  }, [projectId, snapshot, visibleLocalFile])
  const openWebLink = (event: MouseEvent<HTMLElement>) => {
    const bridge = sharedBrowser()
    const anchor = (event.target as Element).closest?.('a[href]') as HTMLAnchorElement | null
    if (!bridge || event.defaultPrevented || event.button > 1 || !anchor || anchor.hasAttribute('download') || !/^https?:/i.test(anchor.href)) return
    event.preventDefault()
    const sessionId = conversation.session?.session_id
    if (sessionId) showPanel('browser')
    void bridge.command(sessionId ? { action: 'create', session_id: sessionId, url: anchor.href }
      : { action: 'open-external', url: anchor.href }).catch(error => notify('warn', authorFacingError(error, '网页没有打开，请重试。')))
  }
  const showChange = useCallback((changeSetId: string) => {
    setChangeTarget({ id: changeSetId, sessionId: conversation.session?.session_id ?? null, projectId })
    setChangesFocusRequest((value) => value + 1)
    showPanel('changes')
  }, [showPanel, conversation.session?.session_id, projectId])

  // ---- 对话与项目 ----
  type NewChatRequest = { projectId: string | null; restoreDraft?: (projectId: string | null, sessionId: string) => void }
  const afterSwitch = useRef<NewChatRequest | null>(null)
  const [pendingTick, setPendingTick] = useState(0)
  const queueNewChat = useCallback((next: NewChatRequest | null) => {
    afterSwitch.current = next
    setPendingTick((value) => value + 1)
  }, [])

  const openProject = useCallback((nextProjectId: string) => {
    setOverlay(null)
    setPreferredSessionId(null)
    setOpenPath(null)
    workspace.openBook(nextProjectId)
  }, [setOverlay, workspace])

  const openConversation = useCallback((row: ConversationRow) => {
    setOverlay(null)
    if (row.projectId === projectId) {
      void conversation.selectConversation(row.sessionId).catch((error: unknown) => notify('warn', authorFacingError(error, T.agent.switchFailed)))
      return
    }
    setPreferredSessionId(row.sessionId)
    setOpenPath(null)
    queueNewChat(null)
    if (row.projectId) workspace.openBook(row.projectId)
    else workspace.closeBook()
  }, [conversation, notify, projectId, queueNewChat, setOverlay, workspace])

  const newConversation = useCallback((nextProjectId: string | null, restoreDraft?: NewChatRequest['restoreDraft']) => {
    setOverlay(null)
    if (nextProjectId === projectId) {
      void conversation.createConversation().then((created) => {
        if (created) restoreDraft?.(nextProjectId, created.session_id)
        conversations.refresh()
      }).catch((error: unknown) => notify('danger', authorFacingError(error, T.agent.newChatFailed)))
      return
    }
    setPreferredSessionId(null)
    setOpenPath(null)
    queueNewChat({ projectId: nextProjectId, restoreDraft })
    if (nextProjectId) workspace.openBook(nextProjectId)
    else workspace.closeBook()
  }, [conversation, conversations, notify, projectId, queueNewChat, setOverlay, workspace])

  const idle = conversation.contextState === 'idle' && !conversation.switchingSession && conversation.availability === 'available'
  useEffect(() => {
    const pending = afterSwitch.current
    if (!pending || pending.projectId !== projectId || !idle) return
    afterSwitch.current = null
    void conversation.createConversation().then((created) => {
      if (created) pending.restoreDraft?.(pending.projectId, created.session_id)
      conversations.refresh()
    }).catch((error: unknown) => {
      // 换项目和排队是同一次渲染：对话链可能才开始读新项目的对话，忙就接着排，闲下来这里再跑一次。
      if (error instanceof AgentBusyError) {
        if (!afterSwitch.current) afterSwitch.current = pending
        return
      }
      notify('danger', authorFacingError(error, T.agent.newChatFailed))
    })
  }, [conversation, conversations, idle, notify, pendingTick, projectId])

  const folderOpened = useCallback((nextProjectId: string, after: 'new-chat' | 'move-chat') => {
    setOverlay(null)
    setOpenPath(null)
    conversations.refresh()
    const sessionId = conversation.session?.session_id
    const restoreDraft = after === 'move-chat' ? composerRef.current?.captureDraftTransfer() : undefined
    if (after === 'move-chat' && sessionId) {
      void conversations.rebind(sessionId, nextProjectId)
        .then(() => {
          restoreDraft?.(nextProjectId, sessionId)
          setPreferredSessionId(sessionId)
          workspace.openBook(nextProjectId)
          notify('ok', T.agent.boundToBook)
        })
        .catch((error: unknown) => notify('danger', authorFacingError(error, T.agent.rebindFailed)))
      return
    }
    setPreferredSessionId(null)
    queueNewChat({ projectId: nextProjectId, restoreDraft })
    workspace.openBook(nextProjectId)
  }, [conversation.session, conversations, notify, queueNewChat, setOverlay, workspace])

  const rebindConversation = useCallback(async (target: string | null) => {
    const sessionId = conversation.session?.session_id
    const restoreDraft = composerRef.current?.captureDraftTransfer()
    if (!sessionId) { newConversation(target, restoreDraft); return }
    await conversations.rebind(sessionId, target)
    restoreDraft?.(target, sessionId)
    setPreferredSessionId(sessionId)
    setOpenPath(null)
    if (target) workspace.openBook(target)
    else workspace.closeBook()
    notify('ok', target ? T.agent.boundToBook : T.agent.unbound)
  }, [conversation.session, conversations, newConversation, notify, workspace])

  // 左栏跟着对话变化刷新（新建、改名、删除、第一轮收尾时后台起的名字）。
  const sessionCount = conversation.sessions.length
  const activeSessionId = conversation.session?.session_id ?? null
  const fileChangesRefreshKey = `${conversation.activeRun?.run_id ?? ''}:${conversation.activeRun?.status ?? ''}:${conversation.activities.filter((activity) => activity.kind === 'tool_result').length}`
  const sessionFileChanges = useSessionFileChanges(activeSessionId, fileChangesRefreshKey, workspace.refresh,
    panelShown && effectivePanelTab === 'changes')
  const refreshConversations = conversations.refresh
  useEffect(() => { refreshConversations() }, [refreshConversations, sessionCount, activeSessionId])
  const wasBusy = useRef(false)
  useEffect(() => {
    if (wasBusy.current && !conversation.busy) refreshConversations()
    wasBusy.current = conversation.busy
  }, [conversation.busy, refreshConversations])

  const providerChanged = useCallback(async () => {
    // 设置页管理服务及新对话默认值；已有对话只在聊天模型选择器里主动换模型。
    await provider.reload({ throwOnError: true })
  }, [provider])

  const [lastSettings, setLastSettings] = useState<SettingsSection>(() => {
    try {
      const saved = window.localStorage.getItem('zhimeng.app.settings-section')
      if (saved === 'appearance' || saved === 'behavior') return 'general'
      return SETTINGS_PAGES.find((page) => page.key === saved)?.key ?? 'models'
    } catch { return 'models' }
  })
  const openSettings = useCallback((section: SettingsSection = lastSettings) => {
    setLastSettings(section)
    try { window.localStorage.setItem('zhimeng.app.settings-section', section) } catch { /* Keep this window usable. */ }
    setOverlay({ kind: 'settings', section })
  }, [lastSettings, setOverlay])
  const showCommands = useCallback((id?: string) => {
    setCommandFocus((current) => ({ sessionId: activeSessionId, id: id ?? null, request: current.request + 1 }))
    showPanel('commands')
  }, [activeSessionId, showPanel])
  const openMemorySettings = useCallback(() => openSettings('memory'), [openSettings])
  return (
    <WorkbenchLayout
      className="zm"
      sideCollapsed={sideCollapsed}
      panelShown={panelShown}
      panelWide={panelWide}
      settingsOpen={overlay?.kind === 'settings'}
      onClick={openWebLink}
      onAuxClick={openWebLink}
      data-theme={theme}
      data-side={sideCollapsed ? 'off' : 'on'}
      data-panel={panelShown ? (panelWide ? 'wide' : 'on') : 'off'}
      data-pane={pane}
      data-settings={overlay?.kind === 'settings' ? 'open' : undefined}
      style={{ '--zm-ui': `${uiSizePixels(uiSize)}px` } as CSSProperties}
    >
      {sideCollapsed ? (
        <nav className="zm-side zm-rail" aria-label={A.sidebar.label}>
          <IconBtn label={A.sidebar.expand} icon={<PanelLeftOpen />} onClick={() => setSideCollapsed(false)} />
          <IconBtn label={A.sidebar.newChat} icon={<SquarePen />} onClick={() => newConversation(null)} />
          <IconBtn label={A.sidebar.search} icon={<Search />} onClick={() => setSideCollapsed(false)} />
          <span className="zm-spacer" />
          <IconBtn label={A.sidebar.settings} icon={<Settings />} onClick={() => openSettings()} />
        </nav>
      ) : (
        <Sidebar
          conversations={conversations}
          activeSessionId={activeSessionId}
          activeProjectId={projectId}
          onOpenConversation={openConversation}
          onNewConversation={newConversation}
          onRenameConversation={(row, title) => {
            void conversation.renameConversation(row.sessionId, title).then(() => conversations.refresh()).catch((error: unknown) => notify('danger', authorFacingError(error, A.sidebar.renameFailed)))
          }}
          onDeleteConversation={(row) => askConfirm({
            title: A.sidebar.deleteTitle,
            body: A.sidebar.deleteBody,
            danger: true,
            confirmText: A.common.delete,
            onConfirm: () => conversation.deleteConversation(row.sessionId).then(() => conversations.refresh()),
          })}
          onOpenProject={openProject}
          onNewFolder={() => setOverlay({ kind: 'folder', mode: 'new', after: 'new-chat' })}
          onOpenFolder={() => setOverlay({ kind: 'folder', mode: 'open', after: 'new-chat' })}
          onRenameProject={(id, name) => {
            void workspace.renameBook(id, name).then(() => conversations.refresh()).catch((error: unknown) => notify('danger', authorFacingError(error, A.sidebar.renameFailed)))
          }}
          onRevealProject={(id) => { void revealProject(id).catch((error: unknown) => notify('danger', authorFacingError(error, A.sidebar.revealFailed))) }}
          onHideProject={(id) => { if (id === projectId) newConversation(null) }}
          onOpenSettings={() => openSettings()}
          onCollapse={() => setSideCollapsed(true)}
          notify={notify}
        />
      )}

      <main className="zm-main">
        {overlay?.kind === 'settings' ? (
          <Suspense fallback={<div className="zm-page-loading" role="status">{A.common.loading}</div>}>
          <SettingsView
            section={overlay.section}
            onSection={openSettings}
            onClose={() => setOverlay(null)}
            runtime={runtime}
            theme={themeChoice}
            onTheme={setThemeChoice}
            provider={provider}
            onProviderChanged={providerChanged}
            projectSnapshot={snapshot}
            onProjectSaved={workspace.refresh}
            onOpenProjectFile={(path) => { setOverlay(null); openFile(path) }}
            notify={notify}
            askConfirm={askConfirm}
          />
          </Suspense>
        ) : null}
        {overlay?.kind === 'folder' ? (
          <Suspense fallback={<div className="zm-page-loading" role="status">{A.common.loading}</div>}>
          <FolderPicker
            key={`${overlay.mode}-${overlay.after}`}
            mode={overlay.mode}
            openFolder={workspace.openFolder}
            scanProgress={workspace.scanProgress}
            onOpened={(id) => folderOpened(id, overlay.after)}
            onCancel={() => setOverlay(null)}
          />
          </Suspense>
        ) : null}
        <div className="zm-chat-preserved" hidden={overlay !== null}>
          <ChatView
            composerRef={composerRef}
            conversation={conversation}
            projectId={projectId}
            snapshot={snapshot}
            projectState={workspace.state}
            projectError={workspace.error}
            openFileLabel={visibleOpenPath}
            onProjectChanged={workspace.refresh}
            onChangesChanged={review.refresh}
            changesRefreshKey={`${review.records?.map((record) => `${record.change_set_id}:${record.review_revision}:${record.status}`).join('|') ?? ''}:${sessionFileChanges.records.map((record) => `${record.change_id}:${record.status}:${record.undone_at}:${record.files.map((entry) => entry.undone_at).join(',')}`).join('|')}`}
            provider={provider}
            onProviderChanged={providerChanged}
            runtime={runtime}
            projects={conversations.projects}
            onRebindProject={rebindConversation}
            onOpenFolder={() => setOverlay({ kind: 'folder', mode: 'open', after: 'move-chat' })}
            onNewFolder={() => setOverlay({ kind: 'folder', mode: 'new', after: 'move-chat' })}
            onOpenFile={openFile}
            onOpenLocalFile={openLocalFile}
            onShowChange={showChange}
            onShowChanges={() => showPanel('changes')}
            onShowCommands={showCommands}
            onShowBrowser={() => showPanel('browser')}
            onOpenSettings={() => openSettings('models')}
            onOpenMemorySettings={openMemorySettings}
            onOpenSkillsSettings={() => openSettings('skills')}
            panelOpen={panelShown}
            onTogglePanel={() => (panelShown ? closePanel() : showPanel(panelTab))}
            sidebarCollapsed={sideCollapsed}
            onExpandSidebar={() => setSideCollapsed(false)}
            onNewConversation={() => newConversation(projectId)}
            notify={notify}
            askConfirm={askConfirm}
          />
        </div>
      </main>

      {panelShown ? (
        <WorkspacePanel tab={effectivePanelTab} onTab={setPanelTab}
          title={conversation.session?.title || '当前任务'} wide={panelWide}
          onToggleWide={() => setPanelWide(!panelWide)} onClose={closePanel} onReturn={returnToConversation}
          files={projectId && snapshot ? {
            projectId, snapshot, openPath,
            focus: fileFocus?.projectId === projectId && fileFocus.path === openPath ? fileFocus : undefined,
            onOpenPath: (path, location) => path ? openFile(path, location) : setOpenPath(null),
            onSaved: workspace.refresh, notify,
            onOpenLocalFile: openLocalFile,
            onQuote: conversation.switchingSession || conversation.contextState === 'restoring' ? undefined : (quote) => {
              composerRef.current?.quoteFile(quote)
              setPanelWide(false)
              setPane('chat')
            },
          } : null}
          localFile={visibleLocalFile ? {
            target: visibleLocalFile,
            refreshKey: `${fileChangesRefreshKey}:${sessionFileChanges.records.map(record => `${record.change_id}:${record.status}`).join('|')}`,
            onOpenFile: openLocalFile, onBack: backFromLocalFile, notify,
            onQuote: conversation.switchingSession || conversation.contextState === 'restoring' ? undefined : quote => {
              composerRef.current?.quoteFile(quote)
              setPanelWide(false); setPane('chat')
            },
          } : null}
          projectChanges={projectId && snapshot ? {
            snapshot, review, focusId: changesFocus?.startsWith('filechange-') ? null : changesFocus,
            focusRequest: changesFocusRequest, onRefreshProject: workspace.refresh, notify, askConfirm,
          } : null}
          sessionChanges={{ sessionId: activeSessionId, review: sessionFileChanges, focusId: changesFocus, focusRequest: changesFocusRequest }}
          commands={{ sessionId: activeSessionId, focusId: commandFocus.sessionId === activeSessionId ? commandFocus.id : null, focusRequest: commandFocus.request, onChanged: workspace.refresh }}
        />
      ) : null}

      <nav className="zm-pane-switch" aria-label={A.pane.label}>
        <button type="button" className={pane === 'nav' ? 'is-active' : ''} onClick={() => setPane('nav')}>{A.pane.nav}</button>
        <button type="button" className={pane === 'chat' ? 'is-active' : ''} onClick={() => setPane('chat')}>{A.pane.chat}</button>
        <button type="button" className={pane === 'panel' ? 'is-active' : ''} disabled={!projectId && !activeSessionId} onClick={() => showPanel(panelTab)}>{A.pane.panel}</button>
      </nav>

      <Toasts toasts={toasts} onDismiss={dismiss} closeLabel={A.common.close} />
      {confirm ? (
        <ConfirmDialog
          request={{ ...confirm, confirmText: confirm.confirmText ?? A.common.ok }}
          busy={confirmBusy}
          cancelText={A.common.cancel}
          onConfirm={() => void runConfirm()}
          onCancel={() => { if (!confirmBusy) setConfirm(null) }}
        />
      ) : null}
    </WorkbenchLayout>
  )
}

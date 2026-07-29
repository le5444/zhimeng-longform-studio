import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  BookOpen,
  Bot,
  Database,
  Library,
  Menu,
  PenLine,
  Search,
  Settings,
  SlidersHorizontal,
  Sparkles,
  TextCursorInput,
} from 'lucide-react'
import { Bookshelf } from './components/Bookshelf'
import { SettingsDialog } from './components/SettingsDialog'
import { EditorPane } from './components/EditorPane'
import { Inspector, type InspectorTab } from './components/Inspector'
import { Navigator } from './components/Navigator'
import { RunDrawer } from './components/RunDrawer'
import { draftNextChapter, rewritePassage } from './api/chapterDrafts'
import { useProjectWorkspace } from './hooks/useProjectWorkspace'
import { useAuthorProseSave } from './hooks/useAuthorProseSave'
import { useAgentConversation } from './hooks/useAgentConversation'
import { useAgentCandidateOutput } from './hooks/useAgentCandidateOutput'
import { useChangeSets } from './hooks/useChangeSets'
import { useProviderRecovery } from './hooks/useProviderRecovery'
import { useProviderTurnReceipts } from './hooks/useProviderTurnReceipts'
import { useRuntimeStatus } from './hooks/useRuntimeStatus'
import { useWriterTrace } from './hooks/useWriterTrace'
import { candidateBoundChangeSet } from './lib/candidateChangeSet'
import { resolveInstructionChapter } from './lib/agentIntent'
import { countPlainTextCharacters } from './lib/text'
import './App.css'

type MobilePane = 'outline' | 'editor' | 'inspector'
type EditorViewRequest = {
  mode: 'draft' | 'candidate' | 'diff'
  key: number
  changeSetId?: string | null
}

const railActions: Array<{ label: string; icon: typeof BookOpen; tab?: InspectorTab }> = [
  { label: '作品', icon: BookOpen },
  { label: '资料', icon: Library, tab: 'sources' },
  { label: '知识库', icon: Database, tab: 'knowledge' },
  { label: '改稿', icon: PenLine, tab: 'changes' },
  { label: '全书搜索', icon: Search, tab: 'search' },
]

function taskStatusLabel(status: string | null | undefined): string {
  if (status === 'queued') return '任务排队中'
  if (status === 'running') return '助手工作中'
  if (status === 'paused') return '任务已暂停'
  if (status === 'cancelling') return '正在停止'
  if (status === 'succeeded') return '结果待查看'
  if (status === 'failed') return '任务失败'
  if (status === 'cancelled') return '任务已停止'
  if (status === 'interrupted') return '任务已中断'
  return '助手空闲'
}

function writingServiceLabel(connection: string): string {
  if (connection === 'online') return '写作服务正常'
  if (connection === 'degraded') return '写作服务启动中'
  if (connection === 'checking') return '正在连接写作服务'
  return '写作服务离线'
}

export function App() {
  const { runtime, refresh } = useRuntimeStatus()
  const workspace = useProjectWorkspace(runtime.ready)
  const conversation = useAgentConversation({
    enabled: runtime.ready,
    projectId: workspace.snapshot?.project.project_id ?? null,
    projectRevision: workspace.snapshot?.project.revision ?? null,
    projectFingerprint: workspace.snapshot?.project.fingerprint ?? null,
    activeChapterId: workspace.activeChapterId,
  })
  const candidateOutput = useAgentCandidateOutput(
    conversation.activeRun?.run_id ?? null,
    conversation.activeRun?.status ?? null,
  )
  const providerTurnReceipts = useProviderTurnReceipts(candidateOutput.output)
  const providerRecoveryEligible = Boolean(
    conversation.activeRun
      && ['failed', 'interrupted'].includes(conversation.activeRun.status)
      && conversation.workflow?.run_id === conversation.activeRun.run_id
      && (
        conversation.workflow.provider_boundary !== null
        || conversation.workflow.resume_state === 'provider_dispatch_uncertain'
      ),
  )
  const providerRecovery = useProviderRecovery(
    conversation.activeRun?.run_id ?? null,
    providerRecoveryEligible,
    candidateOutput.output,
  )
  const writerTrace = useWriterTrace(
    conversation.activeRun?.run_id ?? null,
    conversation.activeRun?.status ?? null,
    candidateOutput.output,
  )
  const [mobilePane, setMobilePane] = useState<MobilePane>('editor')
  const [inspectorTab, setInspectorTab] = useState<InspectorTab>('agent')
  const [preferredChangeSetId, setPreferredChangeSetId] = useState<string | null>(null)
  const [preferredChangeSetRequestKey, setPreferredChangeSetRequestKey] = useState(0)
  const [candidateReviewRequestKey, setCandidateReviewRequestKey] = useState(0)
  const [editorViewRequest, setEditorViewRequest] = useState<EditorViewRequest | null>(null)
  const lastAutoOpenedChangeSetRef = useRef<string | null>(null)
  const [changeSetFocusRequest, setChangeSetFocusRequest] = useState<{
    changeSetId: string
    fileId: string | null
    relativePath: string | null
  } | null>(null)
  const changeSetReview = useChangeSets(
    workspace.snapshot?.project.project_id ?? null,
    workspace.refresh,
    preferredChangeSetId,
    preferredChangeSetRequestKey,
  )
  const refreshChangeSets = changeSetReview.refresh
  // fixture 不再作为 Runtime 未就绪或历史项目恢复失败时的自动回退。
  // 作者没有拿到真实项目快照前，只能看到书架/加载/错误状态，避免把示例正文当成自己的书。
  const showDemo = false
  const authorSaveEnabled = workspace.contentState === 'ready' && workspace.content !== null
  const onAuthorProseSaved = useCallback((changeSet: { change_set_id: string }) => {
    workspace.refresh()
    setPreferredChangeSetId(changeSet.change_set_id)
    setPreferredChangeSetRequestKey((value) => value + 1)
    setInspectorTab('changes')
  }, [workspace])
  const authorSave = useAuthorProseSave({
    content: workspace.content,
    enabled: authorSaveEnabled,
    onSaved: onAuthorProseSaved,
  })
  const projectName = workspace.snapshot?.project.display_name ?? (workspace.state === 'error' ? '项目读取失败' : workspace.state === 'loading' ? '正在读取项目' : '示例工作区')
  const chapterNumber = workspace.activeChapter?.number
  const chapterLabel = chapterNumber !== null && chapterNumber !== undefined
    ? `第 ${String(chapterNumber).padStart(3, '0')} 章`
    : '未选择章节'
  const volumeNumber = workspace.snapshot?.volumes.find(
    (volume) => volume.volume_id === workspace.activeChapter?.volume_id,
  )?.number
  const volumeLabel = volumeNumber !== null && volumeNumber !== undefined ? `第 ${volumeNumber} 卷` : '未分卷'
  const characterCount = workspace.content
    ? countPlainTextCharacters(workspace.content.content)
    : 0
  const editorStatus = workspace.state === 'loading'
    ? { kind: 'loading' as const, message: '正在装配项目快照与卷章树' }
    : workspace.state === 'error'
      ? { kind: 'error' as const, message: workspace.error ?? '项目读取失败', onRetry: workspace.refresh }
      : workspace.contentState === 'loading'
        ? { kind: 'loading' as const, message: `正在读取${chapterLabel}` }
        : workspace.contentState === 'error'
          ? { kind: 'error' as const, message: workspace.contentError ?? '章节读取失败', onRetry: workspace.refresh }
          : null

  const selectInspectorTab = useCallback((nextTab: InspectorTab) => {
    setInspectorTab(nextTab)
    if (nextTab === 'changes') refreshChangeSets()
    else setPreferredChangeSetId(null)
  }, [refreshChangeSets])

  const reviewChangeSet = useCallback((changeSetId: string) => {
    setPreferredChangeSetId(changeSetId)
    setPreferredChangeSetRequestKey((value) => value + 1)
    setChangeSetFocusRequest({ changeSetId, fileId: null, relativePath: null })
    setEditorViewRequest((current) => ({
      mode: 'diff',
      key: (current?.key ?? 0) + 1,
      changeSetId,
    }))
    setInspectorTab('changes')
    setMobilePane('editor')
  }, [])

  // Agent 或写作按钮改动了别的章节时，工作区跟随到那一章。
  // 新建章节在作者应用改动前还不存在，因此保留路径，等项目刷新后再定位。
  useEffect(() => {
    if (!changeSetFocusRequest || !workspace.snapshot) return

    let { fileId, relativePath } = changeSetFocusRequest
    const detail = changeSetReview.detail
    if (
      fileId === null
      && relativePath === null
      && detail?.change_set_id === changeSetFocusRequest.changeSetId
    ) {
      const preferredChange = detail.changes.find((change) => {
        const file = workspace.snapshot?.files.find((item) => (
          (change.file_id !== null && item.file_id === change.file_id)
          || item.relative_path === change.relative_path
        ))
        return file?.chapter_id != null
      }) ?? detail.changes[0]
      fileId = preferredChange?.file_id ?? null
      relativePath = preferredChange?.relative_path ?? null
    }

    const targetFile = workspace.snapshot.files.find((file) => (
      (fileId !== null && file.file_id === fileId)
      || (relativePath !== null && file.relative_path === relativePath)
    ))
    if (targetFile?.chapter_id) {
      workspace.setActiveChapterId(targetFile.chapter_id)
      setMobilePane('editor')
      setChangeSetFocusRequest(null)
      return
    }

    if (
      fileId !== changeSetFocusRequest.fileId
      || relativePath !== changeSetFocusRequest.relativePath
    ) {
      setChangeSetFocusRequest({
        changeSetId: changeSetFocusRequest.changeSetId,
        fileId,
        relativePath,
      })
    }
  }, [
    changeSetFocusRequest,
    changeSetReview.detail,
    workspace.snapshot,
    workspace.setActiveChapterId,
  ])

  // 续写下一章：模型产出候选 -> 自动跳到 Diff 让作者拍板。不会自己写盘。
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [draftState, setDraftState] = useState<'idle' | 'drafting'>('idle')
  const [draftError, setDraftError] = useState<string | null>(null)
  const refreshAgentProvider = useCallback(() => {
    setDraftError(null)
    conversation.refresh()
  }, [conversation.refresh])

  const draftNext = useCallback(async () => {
    const project = workspace.snapshot?.project
    if (!project || draftState === 'drafting') return
    if (conversation.availability !== 'available') {
      const message = conversation.availability === 'unavailable'
        ? '请先配置写作模型，设置已为你打开。'
        : conversation.detail || '写作模型状态尚未就绪'
      setDraftError(message)
      if (conversation.availability === 'unavailable') setSettingsOpen(true)
      throw new Error(message)
    }
    setDraftState('drafting')
    setDraftError(null)
    try {
      const draft = await draftNextChapter(project.project_id, {
        baseRevision: project.revision,
        baseFingerprint: project.fingerprint,
      })
      await refreshChangeSets()
      reviewChangeSet(draft.change_set_id)
    } catch (caught) {
      setDraftError(caught instanceof Error ? caught.message : '续写失败')
      throw caught
    } finally {
      setDraftState('idle')
    }
  }, [
    conversation.availability,
    conversation.detail,
    workspace.snapshot,
    draftState,
    refreshChangeSets,
    reviewChangeSet,
  ])
  const draftButtonDisabled = !workspace.snapshot
    || draftState === 'drafting'
    || conversation.availability === 'checking'
    || conversation.availability === 'error'
  const draftButtonLabel = draftState === 'drafting'
    ? '正在写…'
    : conversation.availability === 'unavailable'
      ? '先配置模型'
      : conversation.availability === 'checking'
        ? '检查模型…'
        : '续写下一章'
  const draftButtonTitle = draftError
    ?? (conversation.availability === 'unavailable'
      ? '还没有写作模型，点击打开设置'
      : conversation.availability === 'checking'
        ? '正在检查写作模型'
        : conversation.availability === 'error'
          ? conversation.detail
          : '让写作模型续写下一章，生成待你审阅的候选')

  const prepareAgentInstruction = useCallback(async (instruction: string) => {
    const snapshot = workspace.snapshot
    if (!snapshot) throw new Error('请先打开一本作品')

    const resolution = resolveInstructionChapter(
      snapshot.chapters,
      instruction,
      workspace.activeChapterId,
    )
    if (resolution.kind === 'missing') {
      throw new Error(`这本作品里没有第 ${resolution.requestedNumber} 章`)
    }
    if (resolution.kind === 'ambiguous') {
      throw new Error(`第 ${resolution.requestedNumber} 章在多个分卷中重复，请先在左侧点中目标章节`)
    }

    const targetChapterId = resolution.kind === 'resolved'
      ? resolution.chapter.chapter_id
      : workspace.activeChapterId
    if (resolution.kind === 'resolved') {
      workspace.setActiveChapterId(resolution.chapter.chapter_id)
      setMobilePane('editor')
    }
    await conversation.compileContext(
      instruction,
      undefined,
      null,
      targetChapterId,
    )
  }, [
    conversation,
    workspace.activeChapterId,
    workspace.setActiveChapterId,
    workspace.snapshot,
  ])

  // 重写选中段落：同样只出候选，作者看 Diff 后拍板。
  const rewriteSelectedPassage = useCallback(async (passage: string, instruction: string) => {
    const project = workspace.snapshot?.project
    const file = workspace.content?.file
    if (!project || !file) throw new Error('请先打开一个章节')
    const result = await rewritePassage(project.project_id, {
      baseRevision: project.revision,
      baseFingerprint: project.fingerprint,
      relativePath: file.relative_path,
      fileId: file.file_id,
      passage,
      instruction,
    })
    await refreshChangeSets()
    reviewChangeSet(result.change_set_id)
  }, [workspace.snapshot, workspace.content, refreshChangeSets, reviewChangeSet])

  const candidateChangeSetId = useMemo(() => {
    const candidate = candidateOutput.output
    if (!candidate) return null
    const referenced = [...new Set(candidate.references
      .filter((reference) => reference.reference_kind === 'change_set')
      .map((reference) => reference.reference_id))]
    if (referenced.length === 1) return referenced[0]
    if (referenced.length > 1) return null
    const proposed = [...new Set(conversation.activities
      .filter((activity) => (
        activity.kind === 'change_proposal' && activity.run_id === candidate.run_id
      ))
      .map((activity) => activity.kind === 'change_proposal' ? activity.payload.proposal_id : ''))]
    return proposed.length === 1 ? proposed[0] : null
  }, [candidateOutput.output, conversation.activities])
  const editorChangeSet = useMemo(
    () => candidateBoundChangeSet(
      candidateOutput.output,
      candidateChangeSetId,
      changeSetReview.detail,
    ),
    [candidateChangeSetId, candidateOutput.output, changeSetReview.detail],
  )

  useEffect(() => {
    if (
      candidateChangeSetId === null
      || conversation.activeRun?.status !== 'succeeded'
      || lastAutoOpenedChangeSetRef.current === candidateChangeSetId
    ) {
      return
    }
    lastAutoOpenedChangeSetRef.current = candidateChangeSetId
    reviewChangeSet(candidateChangeSetId)
  }, [candidateChangeSetId, conversation.activeRun?.status, reviewChangeSet])

  useEffect(() => {
    lastAutoOpenedChangeSetRef.current = null
  }, [workspace.snapshot?.project.project_id])

  const reviewCandidate = useCallback(() => {
    if (candidateChangeSetId) {
      setPreferredChangeSetId(candidateChangeSetId)
      setPreferredChangeSetRequestKey((value) => value + 1)
    }
    setEditorViewRequest((current) => ({
      mode: 'candidate',
      key: (current?.key ?? 0) + 1,
      changeSetId: candidateChangeSetId,
    }))
    setMobilePane('editor')
  }, [candidateChangeSetId])

  const reviewCandidateEvidence = useCallback(() => {
    setCandidateReviewRequestKey((value) => value + 1)
  }, [])

  const openSearchHit = useCallback((sourceId: string, _relativePath: string | null) => {
    const snapshot = workspace.snapshot
    if (!snapshot) return
    const file = snapshot.files.find((item) => item.file_id === sourceId)
    if (!file?.chapter_id) return
    workspace.setActiveChapterId(file.chapter_id)
    setMobilePane('editor')
  }, [workspace])

  // 只要还没有拿到真实项目快照，就留在书架。即使 localStorage 里记着上次的作品，
  // Runtime 启动/离线期间也不再用可编辑 fixture 顶替真实内容。
  const showBookshelf = workspace.snapshot === null

  if (showBookshelf) {
    return (
      <div className="app-shell app-shell-shelf">
        <header className="topbar">
          <div className="brand-mark"><Sparkles size={17} /><span>织梦</span></div>
          <div className="top-run-metrics">
            <span className={`connection-state state-${runtime.connection}`}><i />{runtime.connection === 'online' ? 'Runtime 就绪' : runtime.connection === 'degraded' ? 'Runtime 启动中' : runtime.connection === 'checking' ? '检查 Runtime' : 'Runtime 离线'}</span>
          </div>
          <div className="top-actions">
            <button className="icon-button" type="button" aria-label="工作台设置" title="写作模型设置" onClick={() => setSettingsOpen(true)}><Settings size={16} /></button>
          </div>
        </header>
        <Bookshelf
          bindings={workspace.bindings}
          loading={workspace.state === 'loading' || !runtime.ready}
          error={workspace.error}
          onOpen={workspace.openBook}
          onAdd={workspace.addBook}
          onRefresh={workspace.refresh}
        />
        <SettingsDialog
          open={settingsOpen}
          onClose={() => setSettingsOpen(false)}
          onSaved={refreshAgentProvider}
        />
      </div>
    )
  }

  return (
    <div className="app-shell" data-mobile-active={mobilePane}>
      <header className="topbar">
        <div className="brand-mark"><Sparkles size={17} /><span>织梦</span></div>
        <div className="breadcrumb"><button className="icon-button top-menu" type="button" aria-label="返回书架" title="返回书架" onClick={workspace.closeBook}><Menu size={17} /></button><div><button type="button" className="breadcrumb-project" onClick={workspace.closeBook} title="返回书架">{projectName}</button><span>/</span><span>{volumeLabel}</span><span>/</span><b>{chapterLabel}</b></div></div>
        <div className="top-run-metrics">
          <span className={`connection-state state-${runtime.connection}`}><i />{writingServiceLabel(runtime.connection)}</span>
          <span><b>助手</b> {taskStatusLabel(conversation.activeRun?.status)}</span>
        </div>
        <div className="top-actions">
          <button className="draft-button" type="button" disabled={draftButtonDisabled} title={draftButtonTitle} onClick={() => { void draftNext().catch(() => undefined) }}><PenLine size={15} /><span>{draftButtonLabel}</span></button>
          <button className="run-button" type="button" disabled={!workspace.snapshot} title="打开写作助手" onClick={() => { setMobilePane('inspector'); selectInspectorTab('agent') }}><Bot size={15} /><span>助手</span></button>
          <button className="icon-button" type="button" aria-label="全局搜索" title="全局搜索" disabled={!runtime.ready || !workspace.snapshot} onClick={() => { setMobilePane('inspector'); selectInspectorTab('search') }}><Search size={16} /></button>
          <button className="icon-button" type="button" aria-label="工作台设置" title="写作模型设置" onClick={() => setSettingsOpen(true)}><Settings size={16} /></button>
        </div>
      </header>

      <nav className="mobile-pane-switcher" role="tablist" aria-label="移动端工作区">
        <button type="button" role="tab" aria-selected={mobilePane === 'outline'} className={mobilePane === 'outline' ? 'is-active' : ''} onClick={() => setMobilePane('outline')}><BookOpen size={15} />章节</button>
        <button type="button" role="tab" aria-selected={mobilePane === 'editor'} className={mobilePane === 'editor' ? 'is-active' : ''} onClick={() => setMobilePane('editor')}><TextCursorInput size={15} />正文</button>
        <button type="button" role="tab" aria-selected={mobilePane === 'inspector'} className={mobilePane === 'inspector' ? 'is-active' : ''} onClick={() => setMobilePane('inspector')}><Bot size={15} />助手{showDemo && <span className="mobile-alert">2</span>}</button>
      </nav>

      <div className="main-grid">
        <nav className="rail" aria-label="主导航">
          <div className="rail-main">{railActions.map((action) => { const Icon = action.icon; const active = action.tab ? inspectorTab === action.tab : action.label === '作品' && !['sources', 'knowledge', 'skills'].includes(inspectorTab); return <button type="button" className={`rail-button${active ? ' is-active' : ''}`} title={action.label} aria-label={action.label} key={action.label} onClick={() => { if (action.tab) { selectInspectorTab(action.tab); setMobilePane('inspector') } else if (action.label === '作品') { workspace.closeBook() } }}><Icon size={18} /></button> })}</div>
          <div className="rail-bottom"><button className={`rail-button${inspectorTab === 'more' ? ' is-active' : ''}`} type="button" title="更多能力" aria-label="更多能力" onClick={() => { selectInspectorTab('more'); setMobilePane('inspector') }}><SlidersHorizontal size={18} /></button><button className="rail-button" type="button" title="写作模型设置" aria-label="写作模型设置" onClick={() => setSettingsOpen(true)}><Settings size={18} /></button></div>
        </nav>
        <Navigator
          snapshot={workspace.snapshot}
          activeChapterId={workspace.activeChapterId}
          projectState={workspace.state}
          demo={showDemo}
          onChapterSelect={workspace.setActiveChapterId}
        />
        <EditorPane
          content={showDemo ? null : workspace.content?.content ?? ''}
          readOnly={showDemo ? false : workspace.contentState !== 'ready'}
          demo={showDemo}
          fileId={workspace.content?.file.file_id ?? null}
          relativePath={workspace.content?.file.relative_path ?? null}
          candidateOutput={candidateOutput.output}
          changeSet={editorChangeSet}
          viewRequest={editorViewRequest}
          onReviewChangeSet={reviewChangeSet}
          onReviewCandidateEvidence={reviewCandidateEvidence}
          onSave={
            authorSaveEnabled
              // authorSave.save 返回 ChangeSet|null，EditorPane 只要 void，
              // 这里显式吞掉返回值，别让类型不匹配一直挂在 typecheck 里。
              ? async (plainText: string) => { await authorSave.save(plainText) }
              : undefined
          }
          onRewritePassage={authorSaveEnabled ? rewriteSelectedPassage : undefined}
          saveState={authorSave.saveState}
          saveError={authorSave.saveError}
          status={editorStatus}
        />
        <Inspector
          runtime={runtime}
          onRefreshRuntime={refresh}
          demo={showDemo}
          chapterLabel={chapterLabel}
          projectId={workspace.snapshot?.project.project_id ?? null}
          projectRevision={workspace.snapshot?.project.revision ?? null}
          projectFingerprint={workspace.snapshot?.project.fingerprint ?? null}
          activeChapterId={workspace.activeChapterId}
          projectError={workspace.error ?? workspace.contentError}
          onRefreshProject={workspace.refresh}
          conversation={conversation}
          activeTab={inspectorTab}
          onTabChange={selectInspectorTab}
          preferredChangeSetId={preferredChangeSetId}
          preferredChangeSetRequestKey={preferredChangeSetRequestKey}
          changeSetReview={changeSetReview}
          candidateOutput={candidateOutput}
          providerRecovery={providerRecovery}
          onReviewCandidate={reviewCandidate}
          onOpenSearchHit={openSearchHit}
          onPrepareInstruction={prepareAgentInstruction}
          onDraftNextChapter={draftNext}
          onOpenSettings={() => setSettingsOpen(true)}
        />
      </div>

      <SettingsDialog
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        onSaved={refreshAgentProvider}
      />
      <RunDrawer demo={showDemo} conversation={conversation} onReviewChangeSet={reviewChangeSet} candidateOutput={candidateOutput} providerTurnReceipts={providerTurnReceipts} writerTrace={writerTrace} providerRecovery={providerRecovery} candidateReviewRequestKey={candidateReviewRequestKey} />
      <footer className="statusbar"><span className={`status-connection state-${runtime.connection}`}><i />{writingServiceLabel(runtime.connection)}</span><span>{workspace.snapshot ? '作品已同步' : workspace.state === 'loading' ? '正在读取作品' : workspace.state === 'error' ? '作品读取失败' : '未打开作品'}</span><span>{workspace.snapshot ? (authorSaveEnabled ? '可编辑 · 修改先审阅' : '正文加载中') : showDemo ? '示例数据' : '项目不可用'}</span><span className="status-spacer" /><span>{workspace.snapshot ? `${chapterLabel} · ${characterCount.toLocaleString()} 字` : showDemo ? '示例章节' : '未加载正文'}</span></footer>
    </div>
  )
}

export default App

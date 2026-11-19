import { useCallback, useEffect, useRef, useState, type ComponentProps } from 'react'
import { ArrowLeft, ArrowUpRight, ChevronDown, FileDiff, FileText, RefreshCw, Undo2 } from 'lucide-react'
import { readChangeHistory, readHistoryDetail, undoHistoryChange, type ChangeHistoryDetail, type ChangeHistoryPage, type HistoryChange, type HistoryScope } from '../../api/changeHistory'
import { authorFacingError } from '../../lib/authorFacingError'
import { RuntimeApiError } from '../../api/runtime'
import { DiffView } from '../components/DiffView'
import { shortDay } from '../lib/time'
import { Btn, IconBtn, Spinner } from '../ui'
import { ChangesPanel } from './Changes'
import { ChangeSource } from './ChangeSource'
import './changeWorkspace.css'

export interface ChangeWorkspaceProps {
  project: ComponentProps<typeof ChangesPanel> | null
  session: { sessionId: string | null; focusId?: string | null; focusRequest?: number; onChanged: () => void }
  onOpenFile?: (path: string) => void
  refreshKey?: string
}
const labels: Record<string, string> = { applied: '已保存', undone: '已撤回', rolled_back: '已撤回', partially_undone: '部分已撤回',
  reviewing: '待保存', ready: '待保存', applying: '正在保存', rolling_back: '正在撤回', discarded: '已放弃',
  failed: '保存失败', conflicted: '文件有变化', unverified: '待核对', pending: '待核对' }
const operations: Record<string, string> = { create: '新建', update: '修改', delete: '删除', move: '移动', mkdir: '新建文件夹' }
const name = (path: string) => path.split(/[\\/]/).at(-1) ?? path
function title(change: HistoryChange) {
  let text = change.title || `${change.files.length} 个文件`
  for (const path of change.files.flatMap(f => [f.path, f.target_path]).filter((p): p is string => Boolean(p)).sort((a,b) => b.length-a.length)) text = text.replaceAll(path, name(path))
  return text
}

export function ChangeWorkspace(props: ChangeWorkspaceProps) {
  if (!props.session.sessionId) return props.project ? <ChangesPanel {...props.project} /> : <p className="zm-muted">开始对话后，文件修改会出现在这里。</p>
  return <History key={`${props.session.sessionId}:${props.project?.snapshot.project.project_id ?? ''}`} {...props} sessionId={props.session.sessionId} />
}

function History({ project, session, onOpenFile, refreshKey, sessionId }: ChangeWorkspaceProps & { sessionId: string }) {
  const [scope, setScope] = useState<HistoryScope>('session')
  const [page, setPage] = useState<ChangeHistoryPage | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [stale, setStale] = useState(false)
  const focusId = session.focusId ?? project?.focusId ?? null
  const focusRequest = session.focusRequest ?? project?.focusRequest ?? 0
  const [navigation, setNavigation] = useState({ focusId, focusRequest, selected: focusId })
  const selected = navigation.focusId === focusId && navigation.focusRequest === focusRequest ? navigation.selected : focusId
  const requests = useRef<AbortController | null>(null)
  const select = (id: string | null) => setNavigation({ focusId, focusRequest, selected: id })
  const load = useCallback(async (offset = 0, revision?: string) => {
    requests.current?.abort()
    const controller = new AbortController(); requests.current = controller
    setBusy(true); setError(null)
    if (!offset) setStale(false)
    try {
      const next = await readChangeHistory(sessionId, scope, offset, revision, { signal: controller.signal })
      if (controller.signal.aborted) return
      setPage(previous => offset && previous ? { ...next, changes: [...previous.changes, ...next.changes] } : next)
    } catch (cause) {
      if (!controller.signal.aborted) { setError(authorFacingError(cause)); if (cause instanceof RuntimeApiError && cause.status === 409) setStale(true) }
    } finally { if (!controller.signal.aborted) setBusy(false) }
  }, [sessionId, scope])
  useEffect(() => { void load(); return () => requests.current?.abort() }, [load, refreshKey])
  const switchScope = (next: HistoryScope) => {
    if (next === scope) return
    requests.current?.abort(); setPage(null); setError(null); select(null); setScope(next)
  }
  const changed = () => { project?.review.refresh(); session.onChanged(); void load() }
  return <div className="zm-change-workspace">
    <div className="zm-history-toolbar">
      {selected ? <Btn size="sm" variant="ghost" icon={<ArrowLeft />} onClick={() => select(null)}>全部修改</Btn>
        : <div><strong>文件修改</strong><span>{page ? `${page.total} 次记录` : '记录每次保存与撤回'}</span></div>}
      <IconBtn size="sm" label="刷新修改记录" icon={<RefreshCw />} disabled={busy} onClick={() => { void load(); session.onChanged() }} />
    </div>
    {!selected ? <>
      {project ? <div className="zm-history-scope" role="group" aria-label="修改记录范围">
        <button aria-pressed={scope === 'session'} onClick={() => switchScope('session')}>当前对话</button>
        <button aria-pressed={scope === 'project'} onClick={() => switchScope('project')}>整个项目</button>
      </div> : null}
      {error ? <p role="alert" className="zm-history-error">{error} <button className="zm-link" onClick={() => void load()}>重试</button></p> : null}
      {busy && !page ? <p className="zm-muted"><Spinner /> 正在读取修改…</p> : null}
      {!busy && page?.total === 0 ? <div className="zm-history-empty"><FileDiff aria-hidden /><strong>还没有文件修改</strong><p>保存文件后，可以在这里打开结果、查看差异或撤回。</p></div> : null}
      <ul className="zm-history-list">{page?.changes.map(change => <li key={change.id}>
        <button className="zm-history-record" onClick={() => select(change.id)}>
          <span className="zm-history-record-title">{title(change)}</span>
          <span className={`zm-history-status is-${change.status}`}>{labels[change.status] ?? change.status}</span>
          <span className="zm-history-record-files">{change.files.slice(0,3).map(file => name(file.target_path ?? file.path)).join('、')}{change.files.length>3 ? ` 等 ${change.files.length} 个文件` : ''}</span>
          <time>{shortDay(change.created_at)}</time>
          <span className="zm-history-record-action">查看记录 <ArrowUpRight aria-hidden /></span>
        </button>
      </li>)}</ul>
      {page?.next_offset !== null && page?.next_offset !== undefined ? <Btn size="sm" busy={busy} disabled={stale} onClick={() => void load(page.next_offset!, page.revision)}>显示更多修改</Btn> : null}
    </> : <HistoryDetail key={`${scope}:${selected}`} sessionId={sessionId} id={selected} scope={scope}
      refreshKey={`${refreshKey}:${page?.revision ?? ''}`} project={project} onOpenFile={onOpenFile} onChanged={changed} onSettled={session.onChanged} />}
  </div>
}

function HistoryDetail({ sessionId, id, scope, refreshKey, project, onOpenFile, onChanged, onSettled }: {
  sessionId: string; id: string; scope: HistoryScope; refreshKey: string; project: ChangeWorkspaceProps['project']
  onOpenFile?: (path: string) => void; onChanged: () => void; onSettled: () => void
}) {
  const [detail, setDetail] = useState<ChangeHistoryDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [loading, setLoading] = useState(true)
  const [reload, setReload] = useState(0)
  const alive = useRef(true)
  const settled = useRef(onSettled)
  settled.current = onSettled
  useEffect(() => { alive.current=true; return () => { alive.current=false } }, [])
  useEffect(() => {
    const controller = new AbortController(); setLoading(true); setError(null)
    void readHistoryDetail(sessionId, id, scope, { signal: controller.signal }).then(next => {
      if (!controller.signal.aborted) setDetail(next)
    }, cause => { if (!controller.signal.aborted) { setDetail(null); setError(authorFacingError(cause)) } })
      .finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [sessionId,id,scope,refreshKey,reload])
  async function undo() {
    if (!detail || busy || loading) return
    setBusy(true); setActionError(null)
    try { await undoHistoryChange(detail) }
    catch (cause) { if(alive.current) setActionError(authorFacingError(cause)) }
    finally { if(alive.current) { setBusy(false); setReload(v=>v+1); onChanged() } else settled.current() }
  }
  if (loading && !detail) return <p className="zm-muted"><Spinner /> 正在读取文件状态…</p>
  if (error) return <p role="alert" className="zm-history-error">{error} <button className="zm-link" onClick={()=>setReload(v=>v+1)}>重试</button></p>
  if (!detail) return null
  // Unsaved proposals keep their existing acceptance workflow.
  if (detail.source==='change_set' && ['reviewing','ready'].includes(detail.status) && project && project.snapshot.project.project_id===detail.project_id) {
    return <ChangesPanel {...project} focusId={id} />
  }
  return <div className="zm-history-detail" aria-busy={loading}>
    <div className="zm-history-detail-heading"><FileDiff aria-hidden /><h3>{title(detail)}</h3></div>
    <div className="zm-history-detail-meta"><span className={`zm-history-status is-${detail.status}`}>{labels[detail.status] ?? detail.status}</span><time>{shortDay(detail.created_at)}</time><span>{detail.files.length} 个文件</span></div>
    {loading ? <p className="zm-history-notice" role="status"><Spinner /> 正在刷新文件状态…</p> : null}
    {detail.run_id ? <ChangeSource runId={detail.run_id} sessionId={detail.session_id ?? undefined} projectId={detail.project_id ?? undefined} /> : null}
    {detail.undo_available ? <Btn size="sm" icon={<Undo2 />} busy={busy} disabled={loading} onClick={()=>void undo()}>撤回这次修改</Btn> : null}
    {detail.undo_reason && !['undone','rolled_back'].includes(detail.status) ? <p role="status" className="zm-history-notice">{detail.undo_reason}</p> : null}
    {actionError ? <p role="alert" className="zm-history-error">{actionError}</p> : null}
    {detail.previews.map((file,index)=><article className="zm-history-file" key={`${index}:${file.path}`}>
      <div className="zm-history-file-head"><FileText aria-hidden /><div><strong>{name(file.target_path ?? file.path)}</strong><span>{operations[file.operation] ?? file.operation} · {file.current_state==='file' ? '文件可读取' : file.current_state==='missing' ? '当前不存在' : file.current_state==='directory' ? '文件夹' : '暂时无法读取'}</span></div>
        {file.open_path && onOpenFile ? <IconBtn size="sm" icon={<ArrowUpRight />} label={`打开 ${name(file.open_path)}`} disabled={loading} onClick={()=>onOpenFile(file.open_path!)} /> : null}</div>
      <p className="zm-history-path">{file.path}{file.target_path ? ` → ${file.target_path}` : ''}</p>
      {file.before_characters != null && file.after_characters != null ? <p className="zm-history-path">修改前 {file.before_characters.toLocaleString()} 字符 → 修改后 {file.after_characters.toLocaleString()} 字符</p> : null}
      {file.note ? <p className="zm-history-notice">{file.note}</p> : null}
      {file.hunks.length ? <details className="zm-history-diff" open={detail.previews.length===1}><summary><ChevronDown aria-hidden />查看内容差异</summary>{file.hunks.map((h,i)=><DiffView key={i} before={h.before} after={h.after} />)}</details> : null}
    </article>)}
  </div>
}

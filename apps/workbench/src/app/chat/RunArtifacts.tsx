import { useEffect, useId, useRef, useState } from 'react'
import { ChevronRight, FileCheck2 } from 'lucide-react'
import { readRunArtifacts, type RunArtifact, type RunArtifactsPage } from '../../api/runArtifacts'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { Spinner } from '../ui'
import './runArtifacts.css'

function observationLabel(item: RunArtifact): string {
  const prefix = item.record_state === 'undone' ? '已撤回 · ' : ''
  if (item.matches_recorded_state === null) return `${prefix}暂无法核对`
  if (!item.matches_recorded_state) return `${prefix}与记录不同`
  return `${prefix}${item.expected_state === 'missing' ? '已确认不存在' : '与记录一致'}`
}

function observationDetail(item: RunArtifact): string {
  if (item.expected_state === 'unknown') return '这次写入尚无完整的成功记录。'
  if (item.current.state === 'unverified') {
    const reasons: Record<string, string> = {
      changed_during_read: '读取期间文件发生变化，请重新核对。',
      path_is_link_or_reparse_point: '路径现在包含链接，未自动核对其指向的文件。',
      PermissionError: '当前无法读取文件，请检查访问权限。',
      cancelled: '本次读取已取消。',
    }
    return reasons[item.current.reason ?? ''] ?? '当前无法读取这个路径，不能判断文件是否存在。'
  }
  if (item.current.state === 'missing') return '当前路径不存在。'
  if (item.current.state === 'directory') return '当前路径是文件夹；此处不核对文件夹内的内容。'
  if (item.matches_recorded_state === false) return '当前文件内容与这轮任务留下的版本不同。'
  if (item.matches_recorded_state === null) return '缺少完整版本指纹，无法比较。'
  return '当前文件内容与记录的版本相同。'
}

export function RunArtifacts(props: { sessionId: string; runId: string; refreshKey?: string;
  onOpenFile: (path: string) => void; onShowChange: (id: string) => void; canOpenFile?: (path: string) => boolean }) {
  // Reusing a message component for another conversation must drop its old
  // observations synchronously, before any pending request can resolve.
  return <RunArtifactsContent key={`${props.sessionId}:${props.runId}`} {...props} />
}

function RunArtifactsContent({ sessionId, runId, refreshKey, onOpenFile, onShowChange, canOpenFile }: Parameters<typeof RunArtifacts>[0]) {
  const [open, setOpen] = useState(false)
  const [page, setPage] = useState<RunArtifactsPage | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [stale, setStale] = useState(false)
  const request = useRef<AbortController | null>(null)
  const trigger = useRef<HTMLButtonElement>(null)
  const panelId = useId()
  async function load(more = false) {
    request.current?.abort()
    const controller = new AbortController()
    request.current = controller
    setBusy(true)
    setError(null)
    if (!more) { setPage(null); setStale(false) }
    try {
      const next = await readRunArtifacts(sessionId, runId, more ? page?.next_offset ?? 0 : 0,
        more ? page?.revision : undefined, { signal: controller.signal })
      if (controller.signal.aborted) return
      if (next.session_id !== sessionId || next.run_id !== runId) throw new Error('文件记录归属不一致，请重新读取。')
      setPage((previous) => more && previous ? { ...next, observations: [...previous.observations, ...next.observations] } : next)
    } catch (cause) {
      if (controller.signal.aborted) return
      if (cause instanceof RuntimeApiError && cause.status === 409) setStale(true)
      setError(authorFacingError(cause, '暂时无法读取文件记录。'))
    } finally {
      if (!controller.signal.aborted) setBusy(false)
    }
  }
  useEffect(() => () => request.current?.abort(), [])
  const previousRefresh = useRef(refreshKey)
  useEffect(() => {
    if (previousRefresh.current === refreshKey) return
    previousRefresh.current = refreshKey
    request.current?.abort()
    setBusy(false)
    setStale(true)
  }, [refreshKey])
  function close() {
    request.current?.abort()
    setBusy(false)
    setOpen(false)
    trigger.current?.focus()
  }
  return <section className="zm-run-artifacts" onKeyDown={(event) => {
    if (open && event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); close() }
  }}>
    <button ref={trigger} type="button" className="zm-run-artifacts-toggle" aria-expanded={open} aria-controls={panelId}
      onClick={() => { if (open) close(); else { setOpen(true); void load() } }}>
      <ChevronRight aria-hidden className={open ? 'is-open' : ''} /><FileCheck2 aria-hidden /><span>核对本轮文件</span>
    </button>
    {open ? <div id={panelId} className="zm-run-artifacts-body">
      <p className="zm-muted-line">重新读取本轮留下修改记录的路径。与记录一致不代表任务要求已全部完成。</p>
      <div className="zm-run-artifacts-controls">
        <button type="button" className="zm-link" disabled={busy} onClick={() => void load()}>重新核对</button>
        {busy ? <span role="status"><Spinner />正在读取文件…</span> : null}
      </div>
      {error ? <p role="alert">{error}</p> : null}
      {stale && page ? <p role="status">修改记录已有变化，下面是上次读取的结果，请重新核对。</p> : null}
      {page ? <>
        <p className="zm-muted-line">已读取 {page.observations.length} / {page.total_paths} 个路径 · 最近一批 {new Date(page.observed_at).toLocaleString()}</p>
        {['queued', 'starting', 'running', 'awaiting_approval', 'paused', 'cancelling'].includes(page.run_status)
          ? <p className="zm-muted-line">这轮任务尚未结束，文件和记录可能继续变化。</p> : null}
        {!page.total_paths ? <p>本轮没有可核对的文件修改记录。聊天、只读任务及部分命令或外部工具操作不会产生这类记录。</p> : null}
        <ul>{page.observations.map((item) => <li key={item.path}>
          <div className="zm-run-artifact-heading"><span className="zm-run-artifact-path" title={item.path}>{item.path}</span>
            <span className={item.matches_recorded_state === false ? 'zm-run-artifact-different' : ''}>{observationLabel(item)}</span></div>
          <p className="zm-muted-line">{observationDetail(item)}</p>
          <div className="zm-run-artifact-actions">
            {item.current.state === 'file' && (canOpenFile?.(item.path) ?? true) ? <button type="button" className="zm-link" onClick={() => onOpenFile(item.path)}>打开文件</button> : null}
            <button type="button" className="zm-link" onClick={() => onShowChange(item.receipt_id)}>查看修改与撤回</button>
          </div>
        </li>)}</ul>
        {page.next_offset !== null ? <button type="button" className="zm-link" disabled={busy || stale} onClick={() => void load(true)}>读取更多文件</button> : null}
      </> : null}
    </div> : null}
  </section>
}

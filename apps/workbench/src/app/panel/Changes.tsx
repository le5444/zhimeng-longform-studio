import { useEffect, useMemo, useRef, useState } from 'react'
import { ArrowLeft, FileText, Undo2 } from 'lucide-react'
import type { ProjectInspection } from '../../api/projects'
import type { useChangeSets } from '../../hooks/useChangeSets'
import { DiffView } from '../components/DiffView'
import { changeHeadline, dedupeChangeLines, splitChangeSummary } from '../lib/words'
import { authorFacingErrorText } from '../../lib/authorFacingError'
import { changeSize } from '../chat/ChangeCard'
import { shortDay } from '../lib/time'
import { A } from '../text'
import { Btn, Spinner, type AskConfirm, type Notify } from '../ui'
import { ChangeSource } from './ChangeSource'
import { ChangeRow } from '../components/ChangeRow'

type Review = ReturnType<typeof useChangeSets>

const C = A.changes

/**
 * 右栏「修改」：这个文件夹里的每一次修改（Agent 改的、你在文件页改的），新的在上。
 * 点一条看每个文件改了哪几行；没保存的能保存 / 不要，保存过的能撤回。
 * 以前还有逐处接受 / 不要 / 手改、按「旧正文」批量清理这些，普通 agent 的修改记录用不上，没搬过来。
 */
export function ChangesPanel({ snapshot, review, focusId, focusRequest = 0, onRefreshProject, notify, askConfirm, onBack }: {
  snapshot: ProjectInspection
  review: Review
  /** 对话里点了某张修改卡：打开时选中它。 */
  focusId: string | null
  focusRequest?: number
  onRefreshProject: () => void
  notify: Notify
  askConfirm: AskConfirm
  onBack?: () => void
}) {
  const [view, setView] = useState<'list' | 'detail'>(focusId ? 'detail' : 'list')
  const [requestedId, setRequestedId] = useState(focusId)
  const back = () => { setView('list'); onBack?.() }
  const { select, refresh } = review
  // 修改可能是在别处发生的（对话里、文件页里、撤回命令），打开这一栏先刷一遍，列表才不会停在旧的。
  useEffect(() => { refresh() }, [refresh])
  useEffect(() => {
    if (!focusId) return
    select(focusId)
    setRequestedId(focusId)
    setView('detail')
  }, [focusId, focusRequest, select])

  // 状态从哪变到哪，说一句，并刷新文件夹。
  const lastSeen = useRef<{ id: string; status: string } | null>(null)
  useEffect(() => {
    const current = review.detail
    if (!current) return
    const previous = lastSeen.current
    lastSeen.current = { id: current.change_set_id, status: current.status }
    if (!previous || previous.id !== current.change_set_id || previous.status === current.status) return
    const done = current.status === 'applied' ? C.saved : current.status === 'rolled_back' ? C.undone : current.status === 'discarded' ? C.discarded : null
    if (done) {
      notify('ok', done)
      onRefreshProject()
    }
  }, [notify, onRefreshProject, review.detail])

  const records = review.records
  if (view === 'detail' && (!review.detail || review.detail.change_set_id !== requestedId
    || review.detail.project_id !== snapshot.project.project_id || review.detailState === 'error')) {
    const error = review.selectedId === requestedId ? review.detailError
      ?? (review.detail?.change_set_id === requestedId && review.detail.project_id !== snapshot.project.project_id ? '这条修改不属于现在这个项目' : null) : null
    return <div className="zm-changes">
      <div className="zm-file-head"><Btn size="sm" variant="ghost" icon={<ArrowLeft />} onClick={back}>{C.back}</Btn></div>
      {error ? <p className="zm-error-line" role="alert">{authorFacingErrorText(error, C.loadFailed)} <button type="button" className="zm-link" onClick={() => { if (requestedId) select(requestedId); refresh() }}>{A.common.retry}</button></p>
        : <p className="zm-muted"><Spinner /> {C.loading}</p>}
    </div>
  }
  if (view === 'detail' && review.detail) {
    const detail = review.detail
    const paths = detail.changes.map((change) => change.relative_path)
    const words = changeHeadline({ title: detail.title, origin: detail.origin, file_count: paths.length }, snapshot, paths)
    const lines = dedupeChangeLines(words.detail, splitChangeSummary(detail.summary).text)
    const size = changeSize(detail)
    const busy = review.action !== null
    return (
      <div className="zm-changes">
        <div className="zm-file-head">
          <Btn size="sm" variant="ghost" icon={<ArrowLeft />} onClick={back}>{C.back}</Btn>
          <span className="zm-spacer" />
          {detail.status === 'reviewing' || detail.status === 'ready' ? (
            <>
              <Btn size="sm" variant="ghost" disabled={busy} onClick={() => askConfirm({
                title: C.discardTitle,
                body: C.discardBody,
                confirmText: C.discard,
                danger: true,
                onConfirm: () => review.discardByIds([{ changeSetId: detail.change_set_id, expectedReviewRevision: detail.review_revision }]),
              })}>{C.discard}</Btn>
              <Btn size="sm" variant="primary" busy={review.action?.kind === 'apply'} disabled={busy} onClick={() => void review.apply(true)}>{C.save}</Btn>
            </>
          ) : null}
          {detail.status === 'applied' ? (
            <Btn size="sm" icon={<Undo2 />} busy={review.action?.kind === 'rollback'} disabled={busy} onClick={() => askConfirm({
              title: C.undoTitle,
              body: C.undoBody,
              confirmText: C.undo,
              onConfirm: () => review.rollback(),
            })}>{C.undo}</Btn>
          ) : null}
        </div>
        <div className="zm-changes-body">
          <h3 className="zm-changes-title">{words.headline}</h3>
          <p className="zm-changes-meta">
            <span className={`zm-dot is-${detail.status}`} aria-hidden />
            <span>{C.status[detail.status] ?? detail.status}</span>
            <span>{shortDay(detail.updated_at)}</span>
            {size.removed > 0 ? <span className="is-del">删除 {size.removed.toLocaleString('zh-CN')} 字符</span> : null}
            {size.added > 0 ? <span className="is-add">新增 {size.added.toLocaleString('zh-CN')} 字符</span> : null}
          </p>
          {lines.detail ? <p className="zm-changes-detail">{lines.detail}</p> : null}
          {lines.summary ? <p className="zm-changes-summary">{lines.summary}</p> : null}
          <ChangeSource runId={detail.source_run_id} projectId={detail.project_id} />
          {review.actionError ? <p className="zm-error-line">{authorFacingErrorText(review.actionError, A.common.unknownError)}</p> : null}
          {detail.changes.map((change) => (
            <div key={change.change_id} className="zm-diff">
              <div className="zm-diff-head">
                <FileText aria-hidden />
                <span>{change.relative_path}</span>
                {change.operation === 'delete' ? <span className="zm-chip is-danger">{C.fileDeleted}</span> : null}
                {change.operation === 'create' ? <span className="zm-chip">{C.fileCreated}</span> : null}
                {change.operation === 'move' && change.target_relative_path ? <span className="zm-chip">{C.fileMoved(change.target_relative_path)}</span> : null}
              </div>
              {(change.hunks ?? []).map((hunk) => <DiffView key={hunk.hunk_id} before={hunk.before_text} after={hunk.after_text} />)}
            </div>
          ))}
        </div>
      </div>
    )
  }

  return (
    <div className="zm-changes">
      {review.listState === 'loading' && records.length === 0 ? <p className="zm-muted">{C.loading}</p> : null}
      {review.listState === 'error' ? (
        <p className="zm-muted">{C.loadFailed} <button type="button" className="zm-link" onClick={review.refresh}>{A.common.retry}</button></p>
      ) : null}
      {review.listState === 'ready' && records.length === 0 ? <p className="zm-muted">{C.empty}</p> : null}
      <ChangeList records={records} snapshot={snapshot} selectedId={review.selectedId} onPick={(id) => { setRequestedId(id); select(id); setView('detail') }} />
    </div>
  )
}

function ChangeList({ records, snapshot, selectedId, onPick }: {
  records: Review['records']
  snapshot: ProjectInspection
  selectedId: string | null
  onPick: (id: string) => void
}) {
  const items = useMemo(() => [...records].sort((a, b) => b.created_at.localeCompare(a.created_at)), [records])
  return (
    <ul className="zm-change-list" role="list">
      {items.map((record) => {
        const words = changeHeadline({ title: record.title, origin: record.origin, file_count: record.file_count }, snapshot)
        return (
          <li key={record.change_set_id}>
            <ChangeRow
              title={words.headline}
              detail={words.detail}
              dot={record.status}
              meta={<>{C.status[record.status] ?? record.status} · {shortDay(record.created_at)}</>}
              active={record.change_set_id === selectedId}
              onClick={() => onPick(record.change_set_id)}
            />
          </li>
        )
      })}
    </ul>
  )
}

import { useEffect } from 'react'
import { AlertTriangle, ArrowLeft, FileText, RefreshCw, Undo2 } from 'lucide-react'
import type { useSessionFileChanges } from '../../hooks/useSessionFileChanges'
import { DiffView } from '../components/DiffView'
import { ChangeRow } from '../components/ChangeRow'
import { sessionFileChangeDot, sessionFileChangePaths, sessionFileChangeStatus as statusText, sessionFileChangeTitle, sessionFileOperation } from '../lib/fileChangeLabels'
import { shortDay } from '../lib/time'
import { Btn, Spinner } from '../ui'
import { ChangeSource } from './ChangeSource'

export function SessionChangesPanel({ sessionId, review, focusId = null, focusRequest = 0, onBack }: {
  sessionId: string | null
  review: ReturnType<typeof useSessionFileChanges>
  focusId?: string | null
  focusRequest?: number
  onBack?: () => void
}) {
  const { select } = review
  useEffect(() => { if (focusId?.startsWith('filechange-')) select(focusId) }, [focusId, focusRequest, select])
  const detail = review.detail?.change_id === (onBack ? focusId : review.selectedId) ? review.detail : null
  return (
    <div className="zm-changes zm-session-changes">
      <div className="zm-file-head zm-session-changes-head">
        {review.selectedId || onBack ? <Btn size="sm" variant="ghost" icon={<ArrowLeft />} onClick={() => { select(null); onBack?.() }}>返回记录</Btn> : <span>这场对话的文件修改</span>}
        <span className="zm-spacer" />
        <Btn size="sm" variant="ghost" icon={<RefreshCw />} disabled={review.busy || review.detailLoading} onClick={review.refresh}>刷新</Btn>
      </div>
      {!sessionId ? <p className="zm-muted">开始对话后，文件修改会记录在这里。</p> : null}
      {!review.selectedId && !onBack && sessionId ? <>
        <p className="zm-file-change-note">这里保留文件工具的修改，以及命令明确观察范围内的文件变化。命令范围外和未完成核对的影响不包含在内。</p>
        {review.listState === 'loading' && review.records.length === 0 ? <p className="zm-muted"><Spinner /> 正在读取修改记录…</p> : null}
        {review.listError ? <p role="alert" className="zm-error-line">{review.listError} <button className="zm-link" type="button" onClick={review.refresh}>重试</button></p> : null}
        {review.listState === 'ready' && review.records.length === 0 ? <p className="zm-muted">这场对话还没有文件修改记录。</p> : null}
        <ul className="zm-change-list">
          {review.records.map((record) => <li key={record.change_id}>
            <ChangeRow
              title={sessionFileChangeTitle(record)}
              detail={sessionFileChangePaths(record)}
              dot={sessionFileChangeDot[record.status]}
              meta={<>{statusText[record.status]} · {record.files.length} 项 · {shortDay(record.created_at)}</>}
              onClick={() => select(record.change_id)}
            />
          </li>)}
        </ul>
      </> : null}
      {review.detailLoading ? <p className="zm-muted"><Spinner /> 正在核对文件状态…</p> : null}
      {review.detailError ? <p role="alert" className="zm-error-line">{review.detailError} <button className="zm-link" type="button" onClick={review.refresh}>重试</button></p> : null}
      {detail ? <div className="zm-changes-body" aria-busy={review.detailLoading}>
        <h3 className="zm-changes-title">{sessionFileChangeTitle(detail)}</h3>
        <p className="zm-changes-meta"><span title="这次操作当时的结果；当前文件的对照与撤回状态在下面重新核对。">{statusText[detail.status]}</span><span>{shortDay(detail.created_at)}</span></p>
        <ChangeSource runId={detail.run_id} sessionId={detail.session_id} />
        {!review.detailLoading && detail.status !== 'undone' && (detail.undo_reason || detail.status === 'failed' || detail.status === 'unverified') ?
          <div className="zm-file-change-current" role="status" aria-label="当前文件核对">
            <AlertTriangle aria-hidden />
            <div>
              <strong>{detail.undo_reason ? '当前暂不能撤回' : detail.status === 'failed' ? '这次写入尚未完成' : '先核对当前文件'}</strong>
              <p>{detail.undo_reason || '现有文件与可用备份已保留。请核对下面的记录和文件状态，再继续修改或撤回。'}</p>
            </div>
            <Btn size="sm" variant="ghost" icon={<RefreshCw />} disabled={review.busy || review.detailLoading} onClick={review.refresh}>重新核对</Btn>
          </div> : null}
        {detail.status !== 'undone' ? <Btn size="sm" icon={<Undo2 />} busy={review.busy} disabled={review.busy || review.detailLoading || !detail.undo_available} onClick={() => void review.undo(detail.change_id)}>{detail.status === 'partially_undone' ? '继续撤回剩余文件' : '撤回这次修改'}</Btn> : null}
        {detail.status === 'undone' && detail.undo_reason ? <p className="zm-file-change-note">{detail.undo_reason}</p> : null}
        {review.actionError ? <p role="alert" className="zm-error-line">{review.actionError}</p> : null}
        {!review.detailLoading && detail.previews.map((file) => <div className="zm-diff" key={file.sequence}>
          <div className="zm-diff-head"><FileText aria-hidden /><span>{sessionFileOperation[file.operation]}</span>{file.undone_at ? <span className="zm-chip">已撤回</span> : null}</div>
          <p className="zm-file-change-path">{file.path}{file.target_path ? ` → ${file.target_path}` : ''}</p>
          {file.before_characters != null && file.after_characters != null ? <p className="zm-file-change-note" title="按核验后的文本计数，包含空白，每次换行计 1 字符，不含 BOM。">
            修改前 {file.before_characters.toLocaleString('zh-CN')} 字符 → 修改后 {file.after_characters.toLocaleString('zh-CN')} 字符
          </p> : null}
          {file.preview_note ? <p className="zm-file-change-note">{file.preview_note}</p> : null}
          {file.before_text !== null && file.after_text !== null ? <DiffView before={file.before_text} after={file.after_text} /> : null}
        </div>)}
      </div> : null}
    </div>
  )
}

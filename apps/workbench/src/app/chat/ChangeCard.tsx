import { useEffect, useId, useMemo, useState } from 'react'
import { ArrowUpRight, ChevronDown, ChevronUp, FileText, Undo2 } from 'lucide-react'
import { getChangeSet, type ChangeSet } from '../../api/changeSets'
import type { ProjectInspection } from '../../api/projects'
import type { ChangeCard as Card } from './useChangeCards'
import { diffLines } from '../lib/lineDiff'
import { DiffView } from '../components/DiffView'
import { changeHeadline, pathDisplayName, splitChangeSummary } from '../lib/words'
import { T } from '../strings'
import { Btn, Spinner } from '../ui'
import './changeCard.css'

/** 一次修改删了多少字、加了多少字（按逐行对照数，不算空白）。 */
export function changeSize(record: ChangeSet): { removed: number; added: number } {
  let removed = 0
  let added = 0
  for (const change of record.changes) {
    for (const hunk of change.hunks ?? []) {
      for (const row of diffLines(hunk.before_text, hunk.after_text)) {
        const count = row.text.replace(/\s/g, '').length
        if (row.kind === 'del') removed += count
        else if (row.kind === 'add') added += count
      }
    }
  }
  return { removed, added }
}

/** 原地查看差异，也可单独打开右栏；文件入口与保存、撤回操作始终可用。 */
export function ChangeCard({ card, snapshot, onApprove, onDiscard, onRollback, onShowInPanel, onOpenFile }: {
  card: Card
  snapshot: ProjectInspection | null
  onApprove: () => void
  onDiscard: () => void
  onRollback: () => void
  onShowInPanel: () => void
  onOpenFile?: (path: string) => void
}) {
  const [record, setRecord] = useState<ChangeSet | null>(null)
  const [failed, setFailed] = useState(false)
  const [retry, setRetry] = useState(0)
  const [showDiff, setShowDiff] = useState(false)
  const filesId = useId()
  useEffect(() => { setShowDiff(false) }, [card.changeSetId])
  useEffect(() => {
    let cancelled = false
    setRecord(null)
    setFailed(false)
    getChangeSet(card.changeSetId)
      .then((next) => { if (!cancelled) setRecord(next) })
      .catch(() => { if (!cancelled) setFailed(true) })
    return () => { cancelled = true }
  }, [card.changeSetId, card.status, retry])

  const paths = record ? record.changes.map((change) => change.relative_path) : []
  const size = useMemo(() => record ? changeSize(record) : null, [record])
  const detail = record ? changeHeadline({ title: record.title ?? '', origin: record.origin, file_count: paths.length }, snapshot, paths).detail : null
  const summary = splitChangeSummary(record?.summary).text || detail
  const muted = card.status === 'rolled_back' || card.status === 'discarded'
  const status = card.status === 'applied' ? '已保存'
    : card.status === 'rolled_back' ? '已撤回'
      : card.status === 'discarded' ? '已放弃'
        : (card.status === 'attention' || card.status === 'error') ? card.message ?? ''
          : ''

  return (
    <div className={`zm-change zm-result is-${card.status}${muted ? ' is-muted' : ''}`} aria-label="文件改动">
      <div className="zm-change-head">
        <span className="zm-change-title">{record ? `${paths.length} 个文件改动` : '文件改动'}</span>
        {card.status === 'busy' ? <span className="zm-change-status"><Spinner /> {T.agent.changeBusy}</span>
          : status ? <span className={`zm-change-status${card.status === 'error' ? ' is-danger' : ''}`}>{status}</span> : null}
      </div>
      {size && (size.removed > 0 || size.added > 0) ? (
        <div className="zm-change-restored-size zm-change-size" title="按逐行差异统计增删字符，不计空白。">
          {size.removed > 0 ? <span className="is-del">删除 {size.removed.toLocaleString('zh-CN')} 字符</span> : null}
          {size.added > 0 ? <span className="is-add">新增 {size.added.toLocaleString('zh-CN')} 字符</span> : null}
        </div>
      ) : null}
      {summary && !paths.includes(summary) ? <p className="zm-change-summary">{summary}</p> : null}
      {!record && !failed ? <p className="zm-change-summary">{T.agent.changeLoading}</p> : null}
      {failed ? <p className="zm-change-summary">{T.agent.changeLoadFailed} <button type="button" className="zm-link" onClick={() => setRetry((value) => value + 1)}>重新读取</button></p> : null}
      {paths.length > 0 ? (
        <div className="zm-result-files" id={filesId}>
          {record!.changes.map((change) => {
            const name = pathDisplayName(snapshot, change.relative_path)
            const label = change.operation === 'delete' ? `${name}（${T.changes.fileDeleted}）` : name
            const operation = change.operation === 'create' ? '新建' : change.operation === 'delete' ? '删除' : change.operation === 'move' ? '移动' : '修改'
            const hunks = (change.hunks ?? []).filter((hunk) => hunk.before_text !== '' || hunk.after_text !== '')
            return <div key={change.change_id ?? change.relative_path} className="zm-change-file-section">
              {onOpenFile && change.operation !== 'delete' ? (
                <button type="button" className="zm-result-file" onClick={() => onOpenFile(change.operation === 'move' && record?.status === 'applied' ? change.target_relative_path ?? change.relative_path : change.relative_path)} title={change.relative_path}>
                  <FileText aria-hidden />
                  <span>{label}{change.operation === 'move' && change.target_relative_path ? ` → ${change.target_relative_path}` : ''}</span>
                  <small>{operation}</small>
                </button>
              ) : (
                <span className="zm-result-file" title={change.relative_path}>
                  <FileText aria-hidden />
                  <span>{label}{change.operation === 'move' && change.target_relative_path ? ` → ${change.target_relative_path}` : ''}</span>
                  <small>{operation}</small>
                </span>
              )}
              {showDiff ? (
                <div className="zm-change-diff zm-change-inline-diff" role="region" aria-label={`${name} 的改动`}>
                  {hunks.length > 0 ? hunks.map((hunk) => <DiffView key={hunk.hunk_id} before={hunk.before_text} after={hunk.after_text} />)
                    : <p className="zm-change-summary">{change.operation === 'move' ? '文件内容未改动' : '没有文本差异'}</p>}
                </div>
              ) : null}
            </div>
          })}
        </div>
      ) : null}
      <div className="zm-change-foot">
        <Btn size="sm" variant="ghost" icon={showDiff ? <ChevronUp /> : <ChevronDown />} disabled={!record} aria-expanded={showDiff} aria-controls={filesId} onClick={() => setShowDiff((value) => !value)}>{showDiff ? '收起改动' : '查看改动'}</Btn>
        <Btn size="sm" variant="ghost" icon={<ArrowUpRight />} onClick={onShowInPanel}>在右栏查看</Btn>
        <span className="zm-spacer" />
        {card.status === 'pending' || card.status === 'error' ? (
          <>
            <Btn size="sm" variant="ghost" onClick={onDiscard}>{T.agent.changeDiscard}</Btn>
            <Btn size="sm" variant="primary" onClick={onApprove}>{T.agent.changeApply}</Btn>
          </>
        ) : null}
        {card.status === 'applied' ? <Btn size="sm" variant="ghost" icon={<Undo2 />} onClick={onRollback}>{T.agent.changeRollback}</Btn> : null}
      </div>
    </div>
  )
}

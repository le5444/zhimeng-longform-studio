import { useEffect, useMemo, useState, type ComponentProps } from 'react'
import { RefreshCw } from 'lucide-react'
import { authorFacingErrorText } from '../../lib/authorFacingError'
import { changeHeadline } from '../lib/words'
import { shortDay } from '../lib/time'
import { sessionFileChangeDot, sessionFileChangePaths, sessionFileChangeStatus, sessionFileChangeTitle } from '../lib/fileChangeLabels'
import { A } from '../text'
import { IconBtn, Spinner } from '../ui'
import { ChangeRow } from '../components/ChangeRow'
import { ChangesPanel } from './Changes'
import { SessionChangesPanel } from './SessionChangesPanel'

type ProjectChanges = ComponentProps<typeof ChangesPanel>
type SessionChanges = ComponentProps<typeof SessionChangesPanel>

export function ChangeHistory({ project, session }: { project: ProjectChanges | null; session: SessionChanges }) {
  const focusId = session.focusId ?? project?.focusId ?? null
  const request = session.focusRequest ?? project?.focusRequest ?? 0
  const [navigation, setNavigation] = useState({ focusId, request, selectedId: focusId })
  // Reopening the same chat card is a new navigation request, even after returning to the list.
  // Derive the new selection during this render; sync the stored navigation after commit so
  // parent-driven focus changes never call setState from inside render.
  const navigationMatches = navigation.focusId === focusId && navigation.request === request
  const selectedId = navigationMatches ? navigation.selectedId : focusId
  useEffect(() => {
    if (!navigationMatches) setNavigation({ focusId, request, selectedId: focusId })
  }, [focusId, navigationMatches, request])
  const projectRefresh = project?.review.refresh
  const sessionRefresh = session.review.refresh
  const projectRecords = project?.review.records
  const snapshot = project?.snapshot
  const hasProject = Boolean(project)
  useEffect(() => { projectRefresh?.(); sessionRefresh() }, [projectRefresh, sessionRefresh])
  const items = useMemo(() => [
    ...(projectRecords ?? []).map((record) => {
      const words = changeHeadline(record, snapshot!)
      return { id: record.change_set_id, title: words.headline, detail: words.detail, date: record.created_at,
        status: A.changes.status[record.status] ?? record.status, dot: record.status, scope: '项目文件' }
    }),
    ...session.review.records.map((record) => ({ id: record.change_id,
      title: sessionFileChangeTitle(record),
      detail: sessionFileChangePaths(record), date: record.created_at,
      status: sessionFileChangeStatus[record.status], dot: sessionFileChangeDot[record.status],
      scope: hasProject ? '对话中的其他文件' : '对话文件' })),
  ].sort((a, b) => Date.parse(b.date) - Date.parse(a.date) || a.id.localeCompare(b.id)), [projectRecords, snapshot, session.review.records, hasProject])
  const back = () => setNavigation({ focusId, request, selectedId: null })

  if (selectedId?.startsWith('filechange-')) {
    return <SessionChangesPanel key={selectedId} {...session} focusId={selectedId} onBack={back} />
  }
  if (selectedId && project) return <ChangesPanel key={selectedId} {...project} focusId={selectedId} onBack={back} />

  const sources = [
    ...(project ? [{ label: '项目文件', review: project.review }] : []),
    { label: project ? '对话中的其他文件' : '对话文件', review: session.review },
  ]
  return <div className="zm-changes">
    <div className="zm-file-head"><span>全部修改</span><span className="zm-spacer" />
      <IconBtn size="sm" icon={<RefreshCw />} label="刷新修改记录" onClick={() => { projectRefresh?.(); sessionRefresh() }} />
    </div>
    {sources.map(({ label, review }) => <div key={label}>
      {review.listState === 'loading' ? <p className="zm-muted"><Spinner /> 正在读取{label}…</p> : null}
      {review.listState === 'error' ? <p className="zm-error-line" role="alert">{label}：{authorFacingErrorText(review.listError ?? '', '修改记录读取失败')} <button type="button" className="zm-link" onClick={review.refresh}>重试</button></p> : null}
    </div>)}
    {items.length === 0 && sources.every(({ review }) => review.listState === 'ready') ? <p className="zm-muted">还没有文件修改记录。</p> : null}
    <ul className="zm-change-list">
      {items.map((item) => <li key={item.id}>
        <ChangeRow
          title={item.title}
          detail={item.detail}
          dot={item.dot}
          meta={<>{item.scope} · {item.status} · {shortDay(item.date)}</>}
          onClick={() => setNavigation({ focusId, request, selectedId: item.id })}
        />
      </li>)}
    </ul>
  </div>
}

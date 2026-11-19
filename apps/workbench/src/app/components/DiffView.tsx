import { Fragment, useMemo, useState } from 'react'
import { diffLines, type DiffRow } from '../lib/lineDiff'
import './diffView.css'

const CONTEXT = 3

/** Shared by conversation cards and both file-change panels. */
export function DiffView({ before, after }: { before: string; after: string }) {
  const rows = useMemo(() => diffLines(before, after), [before, after])
  const groups = useMemo(() => {
    const result: { start: number; rows: DiffRow[] }[] = []
    for (let start = 0; start < rows.length;) {
      let end = start + 1
      while (end < rows.length && (rows[end].kind === 'same') === (rows[start].kind === 'same')) end++
      result.push({ start, rows: rows.slice(start, end) })
      start = end
    }
    return result
  }, [rows])
  const [expanded, setExpanded] = useState<{ rows: DiffRow[]; starts: Set<number> } | null>(null)
  const toggle = (start: number) => setExpanded((value) => {
    const starts = new Set(value?.rows === rows ? value.starts : [])
    if (starts.has(start)) starts.delete(start)
    else starts.add(start)
    return { rows, starts }
  })
  const renderRow = (row: DiffRow, index: number) => <div key={index} className={`zm-diff-line is-${row.kind}`}>
    <span className="zm-diff-sign" aria-hidden>{row.kind === 'add' ? '+' : row.kind === 'del' ? '−' : ''}</span>
    <span className="zm-diff-text">{row.text || ' '}</span>
  </div>

  return <div className="zm-diff-lines">
    {groups.map((group) => {
      if (group.rows[0].kind !== 'same' || group.rows.length <= CONTEXT * 2 + 2) {
        return <Fragment key={group.start}>{group.rows.map(renderRow)}</Fragment>
      }
      const open = expanded?.rows === rows && expanded.starts.has(group.start)
      const hidden = group.rows.length - CONTEXT * 2
      return <Fragment key={group.start}>
        {group.rows.slice(0, CONTEXT).map(renderRow)}
        <button type="button" className="zm-diff-context" aria-expanded={open} onClick={() => toggle(group.start)}>
          {open ? '收起' : '展开'} {hidden.toLocaleString('zh-CN')} 行未改动内容
        </button>
        {open ? group.rows.slice(CONTEXT, -CONTEXT).map(renderRow) : null}
        {group.rows.slice(-CONTEXT).map(renderRow)}
      </Fragment>
    })}
  </div>
}

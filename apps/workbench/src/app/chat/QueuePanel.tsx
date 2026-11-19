import { useState } from 'react'
import { ArrowDown, ArrowUp, Check, ChevronDown, ChevronRight, Pause, Pencil, Play, Trash2, X } from 'lucide-react'
import type { QueueState, QueueUpdate } from '../../api/agentQueue'
import { IconBtn, Spinner } from '../ui'
import { T } from '../strings'

export function QueuePanel({ state, busy, error, errorSource, activeRunId, onSteer, onUpdate, onReload }: {
  state: QueueState | null
  busy: boolean
  error: string | null
  errorSource?: 'read' | 'change' | null
  activeRunId?: string | null
  onSteer?: (itemId: string, expectedRunId: string) => Promise<boolean>
  onUpdate: (change: QueueUpdate) => Promise<boolean>
  onReload: () => Promise<void>
}) {
  const Q = T.agent.queue
  const [expanded, setExpanded] = useState(true)
  const [editing, setEditing] = useState<{ id: string; text: string } | null>(null)
  if (!state?.items.length && !error) return null
  const paused = state?.pause_reason
  const onlySteering = Boolean(state?.items.length && state.items.every((item) => item.delivery === 'steer_pending'))
  const unavailable = Boolean(error && (errorSource === 'read' || !state))
  return (
    <section className="zm-queue" aria-label={Q.title}>
      <div className="zm-queue-head">
        <button type="button" className="zm-queue-toggle" aria-expanded={expanded} onClick={() => setExpanded(!expanded)}>
          {expanded ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />}
          <span>{unavailable ? Q.unavailable : Q.count(state?.items.length ?? 0)}</span>
        </button>
        <span className="zm-queue-status">{busy ? <Spinner /> : unavailable ? Q.unavailable : onlySteering ? '等待本轮接收' : paused ? Q.reasons[paused] : Q.waiting}</span>
        {state?.items.length && !onlySteering && !unavailable ? <IconBtn label={paused ? Q.resume : Q.pause} icon={paused ? <Play /> : <Pause />} disabled={busy}
          onClick={() => void onUpdate({ action: paused ? 'resume' : 'pause' })} /> : null}
      </div>
      {error ? <div className="zm-queue-error" role="alert">{error} <button className="zm-link" type="button" onClick={() => void onReload()}>{Q.reload}</button></div> : null}
      {expanded && state ? <ol className="zm-queue-list">{state.items.map((item, index) => {
        const pending = item.delivery === 'steer_pending'
        const locked = item.sending || pending
        return (
        <li key={item.item_id} className="zm-queue-item">
          {editing?.id === item.item_id && !locked ? <div className="zm-queue-edit">
            <textarea aria-label={Q.editContent} value={editing.text} onChange={(event) => setEditing({ id: item.item_id, text: event.target.value })} />
            <IconBtn label={Q.save} icon={<Check />} disabled={busy || (!editing.text.trim() && !item.image_ids?.length)} onClick={() => {
              void onUpdate({ action: 'edit', item_id: item.item_id, message: editing.text }).then((saved) => { if (saved) setEditing(null) })
            }} />
            <IconBtn label={Q.cancelEdit} icon={<X />} onClick={() => setEditing(null)} />
          </div> : <>
            <span className="zm-queue-text">{item.image_ids?.length && item.message === '[图片]' ? '' : item.message}{item.image_ids?.length ? <small> · {item.image_ids.length} 张图片</small> : null}</span>
            <div className="zm-queue-actions">
              {pending ? <span className="zm-queue-status" role="status">待本轮接收</span> : item.sending ? <span className="zm-queue-status">{Q.sending}</span> : null}
              {!pending && activeRunId && onSteer ? <button type="button" className="zm-link zm-queue-steer" disabled={busy || locked}
                title="等当前步骤结束后，送给正在工作的 Agent" onClick={() => void onSteer(item.item_id, activeRunId)}>送入本轮</button> : null}
              <IconBtn size="sm" label={Q.up} icon={<ArrowUp />} disabled={busy || locked || index === 0} onClick={() => void onUpdate({ action: 'move', item_id: item.item_id, direction: -1 })} />
              <IconBtn size="sm" label={Q.down} icon={<ArrowDown />} disabled={busy || locked || index === state.items.length - 1} onClick={() => void onUpdate({ action: 'move', item_id: item.item_id, direction: 1 })} />
              <IconBtn size="sm" label={Q.edit} icon={<Pencil />} disabled={busy || locked} onClick={() => {
                void onUpdate({ action: 'pause' }).then((paused) => { if (paused) setEditing({ id: item.item_id, text: item.message }) })
              }} />
              <IconBtn size="sm" label={Q.remove} icon={<Trash2 />} disabled={busy || locked} onClick={() => void onUpdate({ action: 'remove', item_id: item.item_id })} />
            </div>
          </>}
        </li>
      )})}</ol> : null}
    </section>
  )
}

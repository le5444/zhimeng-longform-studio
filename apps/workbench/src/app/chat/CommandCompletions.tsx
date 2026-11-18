import type { CommandCompletionEvent } from '../../api/backgroundEvents'
import './commandCompletions.css'

export function CommandCompletions({ events, error, onRetry, onOpen, onReview, busy }: { events: CommandCompletionEvent[]; error: string | null; onRetry: () => void; onOpen?: (id?: string) => void; onReview?: (id: string) => void; busy?: boolean }) {
  if (!events.length && !error) return null
  return <details className="zm-command-completions">
    <summary>后台命令{events.length ? ` · ${events.length} 条结果` : ''}{error ? <span>状态待刷新</span> : null}</summary>
    {error ? <p className="zm-muted-line">{error} <button type="button" className="zm-link" onClick={onRetry}>重试</button></p> : null}
    <ul>{events.map((event) => <li key={event.event_id}>
      <div><span className="zm-command-completion-status">{event.status === 'interrupted' ? '运行中断' : event.status === 'cancelled' ? '已停止' : event.exit_code === null ? '已退出' : `退出码 ${event.exit_code}`}</span>
        <span className="zm-faint">{event.delivery_state === 'pending' ? event.payload.automatic_delivery_error ? '命令结果已保存，自动接续暂未开始' : '等待 Agent 接续' : event.delivery_state === 'delivered' ? '结果已交给 Agent' : '未自动接续'}</span>
        {onOpen ? <button type="button" className="zm-link" onClick={() => onOpen(event.command_session_id)}>查看日志</button> : null}</div>
      {event.payload.command ? <code title={event.payload.command}>{event.payload.command}</code> : null}
      {event.delivery_state === 'pending' && typeof event.payload.automatic_delivery_error === 'string' ? <div className="zm-command-completion-recovery"><details><summary>查看原因</summary><p>{event.payload.automatic_delivery_error}</p></details>{onReview ? <button type="button" className="zm-link" disabled={busy} onClick={() => onReview(event.command_session_id)}>核对命令结果</button> : null}</div> : null}
    </li>)}</ul>
  </details>
}

export function CompletionOrigin({ commandIds, onOpen, onContinue, busy }: { commandIds: string[]; onOpen?: (id?: string) => void; onContinue?: () => void; busy?: boolean }) {
  return <div className="zm-completion-origin"><span>命令结束后继续</span>{onOpen && commandIds.map((id, index) => <button key={id} type="button" className="zm-link" onClick={() => onOpen(id)}>{commandIds.length === 1 ? '命令日志' : `命令日志 ${index + 1}`}</button>)}{onContinue ? <button type="button" className="zm-link" disabled={busy} onClick={onContinue}>继续核对结果</button> : null}</div>
}

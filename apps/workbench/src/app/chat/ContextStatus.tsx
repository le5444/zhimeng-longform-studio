import { Archive, ChevronDown, History } from 'lucide-react'
import type { SessionContextState } from '../../api/contextState'
import './contextStatus.css'

export function ContextStatus({ state, error, onRetry }: { state: SessionContextState | null; error: string | null; onRetry: () => void }) {
  const running = state?.run_compaction
  const runningLabel = running?.state === 'running' ? '正在整理本轮工具结果…'
    : running?.state === 'succeeded' ? '本轮工具结果已整理'
      : running?.state === 'failed' ? '本轮整理未完成'
        : running ? '本轮整理已停止' : null
  if (!error && !state?.compacting && !state?.latest_checkpoint && !running) return null
  return <details className="zm-context-status">
    <summary>
      {state?.latest_checkpoint ? <Archive aria-hidden /> : <History aria-hidden />}
      <span>{error ? '上下文状态待刷新' : running?.state === 'running' ? runningLabel : state?.compacting ? '正在压缩较早对话…' : runningLabel ?? (state?.latest_checkpoint ? '摘要与最近对话' : '对话上下文')}</span>
      {state && !state.compacting ? <span className="zm-context-count">{state.recent_message_count} 条近期消息</span> : null}
      <ChevronDown aria-hidden className="zm-context-chevron" />
    </summary>
    <div className="zm-context-body">
      {error ? <p role="alert">状态暂未更新：{error} <button className="zm-link" type="button" onClick={onRetry}>重试</button></p> : null}
      {state ? <>
        {state.latest_checkpoint ? <p>{state.compacted_message_count} 条较早消息已整理为摘要，保留 {state.recent_message_count} 条近期消息。原始对话仍可查看。</p> : null}
        {state.compacting && !error && running?.state !== 'running' ? <p role="status">正在整理目标、已完成事项和待办，最新摘要保存后会在这里更新。</p> : null}
        {running && !error ? <p role="status">{running.state === 'running'
          ? '正在逐段整理大段工具结果，用户要求和工具收据保持原样，原始结果会完整存档。整理完成后继续任务。'
          : running.state === 'succeeded'
            ? `已整理 ${running.folded_results} 条工具结果，原始结果已完整存档，可由 Agent 重新读取。这不代表任务已经完成。`
            : '这次整理没有完成，原始记录仍保留；任务的执行结果请查看对话过程。'}</p> : null}
        {state.latest_checkpoint ? <details className="zm-context-summary"><summary>查看最新对话摘要</summary><div>{state.latest_checkpoint.content}</div></details> : null}
      </> : null}
    </div>
  </details>
}

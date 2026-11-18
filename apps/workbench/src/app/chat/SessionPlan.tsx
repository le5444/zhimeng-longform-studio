import { useEffect, useMemo, useState } from 'react'
import { ChevronRight, ListChecks, RefreshCw } from 'lucide-react'
import type { AgentActivity, AgentRun } from '../../api/agent'
import { readSessionPlan, type SessionPlanView } from '../../api/sessionPlan'
import { IconBtn } from '../ui'
import { PlanChecklist } from './Steps'
import './sessionPlan.css'

export function SessionPlan({ sessionId, run, activities }: {
  sessionId: string
  run: AgentRun | null
  activities: AgentActivity[]
}) {
  const [view, setView] = useState<SessionPlanView | null>(null)
  const [error, setError] = useState(false)
  const [loading, setLoading] = useState(false)
  const [reload, setReload] = useState(0)
  const ownRun = run?.session_id === sessionId ? run : null
  const revision = useMemo(() => {
    const calls = new Set(activities.filter(item => item.session_id === sessionId && item.kind === 'tool_call' && item.payload.tool_name === 'plan_update')
      .map(item => item.kind === 'tool_call' ? `${item.run_id}:${item.payload.tool_call_id}` : ''))
    return activities.filter(item => item.session_id === sessionId && item.kind === 'tool_result' && calls.has(`${item.run_id}:${item.payload.tool_call_id}`))
      .map(item => `${item.run_id}:${item.sequence}`).join('|')
  }, [activities, sessionId])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true)
    void readSessionPlan(sessionId, { signal: controller.signal }).then(next => {
      if (!controller.signal.aborted) { setView(next); setError(false) }
    }).catch(() => {
      if (!controller.signal.aborted) setError(true)
    }).finally(() => {
      if (!controller.signal.aborted) setLoading(false)
    })
    return () => controller.abort()
  }, [sessionId, ownRun?.run_id, ownRun?.status, revision, reload])
  useEffect(() => {
    const refresh = () => setReload(value => value + 1)
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [])

  const plan = view?.session_id === sessionId ? view.plan : null
  if (!plan && !error) return null
  const done = plan?.items.filter(item => item.status === 'completed').length ?? 0
  const current = plan?.items.find(item => item.status === 'in_progress') ?? plan?.items.find(item => item.status === 'pending')
  const live = Boolean(plan && ownRun?.run_id === plan.run_id && ['running', 'starting'].includes(ownRun.status))
  return <section className="zm-session-plan" aria-label="对话工作计划">
    {plan ? <details>
      <summary>
        <ChevronRight className="zm-session-plan-chevron" aria-hidden />
        <ListChecks aria-hidden />
        <span>工作计划</span>
        <span className="zm-session-plan-current" title={current?.content}>{current?.content ?? '全部已标记完成'}</span>
        <span className="zm-session-plan-count" title="Agent 标记的进度，实际结果以文件和执行记录为准">{done}/{plan.items.length}</span>
      </summary>
      <div className="zm-session-plan-body">
        <div className="zm-session-plan-source"><span title={plan.updated_at}>来自第 {plan.run_ordinal} 轮</span>
          <IconBtn size="sm" label="刷新工作计划" icon={<RefreshCw />} disabled={loading} onClick={() => setReload(value => value + 1)} />
        </div>
        <PlanChecklist items={plan.items} live={live && !error} />
      </div>
    </details> : null}
    {error ? <div className="zm-session-plan-error" role="status"><span>工作计划暂时无法刷新{plan ? '，当前为上次读取的记录' : ''}。</span>
      <IconBtn size="sm" label="重试读取工作计划" icon={<RefreshCw />} disabled={loading} onClick={() => setReload(value => value + 1)} />
    </div> : null}
  </section>
}

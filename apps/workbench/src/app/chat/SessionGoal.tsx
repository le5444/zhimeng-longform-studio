import { useEffect, useRef, useState } from 'react'
import type { AgentRun } from '../../api/agent'
import { endSessionGoal, readSessionGoal, type SessionGoal as Goal } from '../../api/sessionGoal'
import './sessionGoal.css'

const labels: Record<Goal['status'], string> = {
  active: '持续执行中', paused: '已暂停', blocked: '需要补充条件', completed: 'Agent 判断完成', ended: '已结束目标',
}

export function SessionGoal({ sessionId, run, available, busy, canStart, onStart, onResume, onPause }: {
  sessionId: string | null
  run: AgentRun | null
  available: boolean
  busy: boolean
  canStart: boolean
  onStart: () => void
  onResume: (goal: Goal) => void
  onPause: () => void
}) {
  const [goal, setGoal] = useState<Goal | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loaded, setLoaded] = useState(!sessionId)
  const [ending, setEnding] = useState(false)
  const [reload, setReload] = useState(0)
  const action = useRef<AbortController | null>(null)
  const ownRun = run?.session_id === sessionId ? run : null
  const working = Boolean(ownRun && !['succeeded', 'failed', 'cancelled', 'interrupted'].includes(ownRun.status))
  useEffect(() => {
    const controller = new AbortController()
    let timer: ReturnType<typeof setTimeout> | undefined
    async function read() {
      if (!sessionId || !available) return
      try {
        const next = await readSessionGoal(sessionId, { signal: controller.signal })
        if (controller.signal.aborted) return
        setGoal(next.goal); setError(null); setLoaded(true)
        if (working || next.goal?.status === 'active') timer = setTimeout(() => void read(), 1500)
      } catch (e) {
        if (!controller.signal.aborted) setError(e instanceof Error ? e.message : '持续目标暂时无法读取。')
      }
    }
    void read()
    return () => { controller.abort(); clearTimeout(timer) }
  }, [sessionId, available, ownRun?.run_id, ownRun?.status, working, reload])
  useEffect(() => {
    const refresh = () => setReload(value => value + 1)
    window.addEventListener('focus', refresh)
    return () => { window.removeEventListener('focus', refresh); action.current?.abort() }
  }, [])
  const current = goal?.session_id === sessionId ? goal : null
  const disabled = !available || busy || ending
  async function end() {
    if (!current || disabled) return
    const controller = new AbortController()
    action.current = controller
    setEnding(true)
    try {
      const next = await endSessionGoal(current, controller.signal)
      if (!controller.signal.aborted) { setGoal(next.goal); setError(null) }
    } catch (e) {
      if (!controller.signal.aborted) { setError(e instanceof Error ? e.message : '目标未能结束。'); setReload(value => value + 1) }
    } finally {
      if (!controller.signal.aborted) setEnding(false)
    }
  }
  return <section className={`zm-session-goal${current ? " has-goal" : " is-idle"}${error ? " has-error" : ""}`} aria-label="持续目标">
    {current ? <details>
      <summary><span>{labels[current.status]}</span><span className="zm-goal-objective" title={current.objective}>{current.objective}</span></summary>
      <div className="zm-goal-body">
        <p>{current.objective}</p>
        {current.progress ? <p><strong>Agent 记录的进度：</strong>{current.progress}</p> : null}
        {current.evidence ? <p><strong>验证或阻塞说明：</strong>{current.evidence}</p> : null}
        {current.pause_reason ? <p>{current.pause_reason}</p> : null}
        <p className="zm-goal-note">已完成 {current.stage_count} 段回复。进度与完成判断来自 Agent，实际结果请结合文件和执行记录核对。</p>
      </div>
    </details> : null}
    <div className="zm-goal-actions">
      {current?.status === 'active' ? <button type="button" className="zm-link" disabled={!available || ending || ownRun?.run_id !== current.run_id || !working} onClick={onPause}>暂停目标</button> : null}
      {current && ['paused', 'blocked', 'completed'].includes(current.status) ? <>
        <button type="button" className="zm-link" disabled={disabled || working || Boolean(error)} onClick={() => onResume(current)}>{current.status === 'completed' ? '继续完善目标' : '继续目标'}</button>
        <button type="button" className="zm-link" disabled={disabled || working || Boolean(error)} onClick={() => void end()}>结束目标</button>
      </> : null}
      {(!current || ['completed', 'ended'].includes(current.status)) ? <button type="button" className="zm-link" disabled={disabled || working || !canStart || !loaded || Boolean(error)} onClick={onStart} aria-label="将输入作为持续目标" title="将输入作为持续目标发送，阶段性回答后继续执行，可随时暂停">持续目标发送</button> : null}
      {current ? <button type="button" className="zm-link" disabled={!available || ending} onClick={() => setReload(value => value + 1)}>刷新目标</button> : null}
    </div>
    {error ? <div role="status" className="zm-goal-error">目标状态暂时无法确认{current ? '，当前显示上次记录' : ''}。{error} <button type="button" className="zm-link" disabled={!available} onClick={() => setReload(value => value + 1)}>重试读取目标</button></div> : null}
  </section>
}

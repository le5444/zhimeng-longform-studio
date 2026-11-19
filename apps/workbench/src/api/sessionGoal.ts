import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type GoalAdmission = { action: 'start' } | { action: 'resume'; goal_id: string; expected_revision: number }
export interface SessionGoal {
  goal_id: string
  session_id: string
  objective: string
  status: 'active' | 'paused' | 'blocked' | 'completed' | 'ended'
  revision: number
  run_id: string
  stage_count: number
  progress: string
  evidence: string
  pause_reason: string | null
  requested_status: 'completed' | 'blocked' | null
  status_source: 'agent' | 'user' | 'runtime'
  created_at: string
  updated_at: string
}
export interface SessionGoalView { session_id: string; goal: SessionGoal | null }

function check(view: SessionGoalView, sessionId: string): SessionGoalView {
  if (view.session_id !== sessionId || (view.goal && view.goal.session_id !== sessionId)) throw new Error('目标不属于当前对话，请重新读取。')
  return view
}
export async function readSessionGoal(sessionId: string, options: RuntimeClientOptions = {}): Promise<SessionGoalView> {
  return check(await fetchRuntimeJson<SessionGoalView>(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/goal`, options), sessionId)
}
export async function endSessionGoal(goal: SessionGoal, signal?: AbortSignal): Promise<SessionGoalView> {
  return check(await fetchRuntimeJson<SessionGoalView>(`/api/v1/agent/sessions/${encodeURIComponent(goal.session_id)}/goal/end`, {},
    { method: 'POST', signal, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ goal_id: goal.goal_id, expected_revision: goal.revision }) }), goal.session_id)
}

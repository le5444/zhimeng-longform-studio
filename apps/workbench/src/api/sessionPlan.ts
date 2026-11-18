import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'
import type { AgentRun } from './agent'

export interface SessionPlanView {
  session_id: string
  plan: {
    run_id: string
    run_ordinal: number
    run_status: AgentRun['status']
    tool_call_id: string
    updated_at: string
    source: 'tool_result' | 'legacy_arguments'
    status_source: 'agent'
    items: { content: string; status: 'pending' | 'in_progress' | 'completed' }[]
  } | null
}

export async function readSessionPlan(sessionId: string, options: RuntimeClientOptions = {}): Promise<SessionPlanView> {
  const view = await fetchRuntimeJson<SessionPlanView>(`/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/plan`, options)
  if (view.session_id !== sessionId || (view.plan !== null && !Array.isArray(view.plan?.items))) throw new Error('工作计划不属于当前对话或内容不完整。')
  return view
}

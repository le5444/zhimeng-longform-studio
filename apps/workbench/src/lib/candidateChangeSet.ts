import type { AgentActivity } from '../api/agent'

/** 这一轮 Agent 提出的那一份修改（只认这一轮自己的提案；一轮提了好几份就不猜）。 */
export function agentRunChangeSetId(
  activeRunId: string | null,
  activities: AgentActivity[],
): string | null {
  if (!activeRunId) return null
  const proposed = [...new Set(activities
    .filter((activity) => activity.kind === 'change_proposal' && activity.run_id === activeRunId)
    .map((activity) => activity.kind === 'change_proposal' ? activity.payload.proposal_id : ''))]
  return proposed.length === 1 ? proposed[0] : null
}

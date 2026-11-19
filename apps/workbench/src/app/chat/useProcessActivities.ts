import { useMemo } from 'react'
import type { AgentActivity } from '../../api/agent'

/**
 * Process views need message boundaries, not every character of the answer.
 * Keep the first assistant delta in each consecutive span: it still separates
 * tool attempts even when a completed message has not arrived. All other
 * records, including completed messages and reasoning deltas, stay intact.
 * This is a view projection; the conversation keeps the full source history.
 */
export function processActivities(activities: AgentActivity[]): AgentActivity[] {
  const ordered = activities.some((item, index) => index > 0 && item.sequence < activities[index - 1].sequence)
    ? [...activities].sort((a, b) => a.sequence - b.sequence) : activities
  const result: AgentActivity[] = []
  let previous: AgentActivity | undefined
  for (const activity of ordered) {
    const delta = activity.kind === 'message' && activity.payload.role === 'assistant' && activity.payload.phase === 'delta'
    const sameSpan = delta && previous?.kind === 'message' && previous.payload.role === 'assistant'
      && previous.payload.phase === 'delta' && previous.run_id === activity.run_id && previous.session_id === activity.session_id
    if (!sameSpan) result.push(activity)
    previous = activity
  }
  return result
}

export function useProcessActivities(activities: AgentActivity[]): AgentActivity[] {
  const select = useMemo(() => {
    let previous: AgentActivity[] = []
    return (source: AgentActivity[]) => {
      const next = processActivities(source)
      if (next.length !== previous.length || next.some((item, index) => item !== previous[index])) previous = next
      return previous
    }
  }, [])
  return useMemo(() => select(activities), [activities, select])
}

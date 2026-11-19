import type { AgentActivity } from '../api/agent'

/** 首条立即显示；突发回放合批追加，保留每条记录和原有顺序。 */
export function createActivityBuffer(publish: (activities: AgentActivity[]) => void) {
  let pending: AgentActivity[] = []
  let timer: ReturnType<typeof setTimeout> | undefined
  const drain = () => {
    if (pending.length === 0) return
    const batch = pending
    pending = []
    publish(batch)
  }
  const flush = () => {
    if (timer !== undefined) clearTimeout(timer)
    timer = undefined
    drain()
  }
  return {
    push(activity: AgentActivity) {
      pending.push(activity)
      if (timer === undefined) {
        drain()
        timer = setTimeout(flush, 32)
      } else if (pending.length >= 250) drain()
    },
    flush,
  }
}

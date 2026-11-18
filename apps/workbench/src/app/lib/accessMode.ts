/** Agent 的工作权限。完全访问是作者工作台的默认：读、改、执行都由 Agent 自己完成。 */
export type AgentAccessMode = 'full' | 'read_only'

const KEY = 'zhimeng.desk.agent-access-mode.v1'

export function readAgentAccessMode(): AgentAccessMode {
  try {
    return window.localStorage.getItem(KEY) === 'read_only' ? 'read_only' : 'full'
  } catch {
    return 'full'
  }
}

export function writeAgentAccessMode(mode: AgentAccessMode): void {
  try {
    window.localStorage.setItem(KEY, mode)
  } catch {
    // 本机存不了就只影响这一次打开
  }
}

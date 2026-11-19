/** AI 修改的保存方式：作者在设置里可选，本机记忆。 */
export type ApplyPolicy = 'auto' | 'review'

const APPLY_POLICY_KEY = 'zhimeng.apply-policy.v1'

export function readApplyPolicy(): ApplyPolicy {
  try {
    return window.localStorage.getItem(APPLY_POLICY_KEY) === 'review' ? 'review' : 'auto'
  } catch {
    return 'auto'
  }
}

export function writeApplyPolicy(policy: ApplyPolicy): boolean {
  try {
    window.localStorage.setItem(APPLY_POLICY_KEY, policy)
  } catch {
    return false
  }
  return true
}

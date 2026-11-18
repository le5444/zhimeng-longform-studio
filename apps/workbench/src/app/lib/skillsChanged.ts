import type { UserSkillsView } from '../../api/userSkills'

const eventName = 'zhimeng:skills-changed'

/** 设置与保留挂载的聊天共享技能清单；完整快照可直接复用，否则重新读取。 */
export function publishSkillsChanged(view?: UserSkillsView) {
  window.dispatchEvent(new CustomEvent(eventName, { detail: { view } }))
}

export function subscribeSkillsChanged(listener: (view?: UserSkillsView) => void) {
  const onChange = (event: Event) => listener((event as CustomEvent<{ view?: UserSkillsView }>).detail?.view)
  window.addEventListener(eventName, onChange)
  return () => window.removeEventListener(eventName, onChange)
}

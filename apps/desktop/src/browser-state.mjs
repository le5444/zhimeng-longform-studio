import { randomUUID } from 'node:crypto'

export class BrowserConflict extends Error {
  constructor(code, message) { super(message); this.code = code; this.status = 409 }
}

// This state belongs to the host, independent of the current UI connection.
export class PageControl {
  constructor() { this.mode = 'user'; this.generation = randomUUID(); this.observation = null }
  invalidate() { this.generation = randomUUID(); this.observation = null }
  transfer(mode) {
    if (!['user', 'agent'].includes(mode)) throw new TypeError('未知控制状态。')
    this.mode = mode; this.invalidate()
  }
  observe() {
    this.observation = randomUUID()
    return { observation_id: this.observation, generation: this.generation }
  }
  require(observation) {
    if (this.mode !== 'agent') throw new BrowserConflict('user_control', '用户正在操作此页面，请等待交还。')
    if (!observation || observation !== this.observation) throw new BrowserConflict('stale_observation', '页面或控制权已变化，请重新观察。')
  }
}

export function pageUrl(raw) {
  const url = new URL(raw)
  if (!['http:', 'https:', 'file:', 'about:'].includes(url.protocol) || (url.protocol === 'about:' && url.href !== 'about:blank')) {
    throw new TypeError('请输入网页地址或本地文件地址。')
  }
  return url.href
}

import { BrowserWindow } from 'electron'

const captureError = (code, message) => Object.assign(new Error(message), { code, status: 409 })

// A background WebContents needs a native compositor surface. Reuse the same
// contents in a transparent, non-interactive window; never reload or clone it.
export class PageCapture {
  constructor(host) { this.host = host; this.tail = Promise.resolve(); this.active = null; this.window = null; this.pending = new Set(); this.closed = false }

  capture(tab) {
    const entry = { tab, controller: new AbortController() }
    if (this.closed) return Promise.reject(captureError('browser_page_closed', '浏览器已关闭。'))
    this.pending.add(entry)
    const task = this.tail.then(() => this.run(entry)).finally(() => this.pending.delete(entry))
    this.tail = task.catch(() => {})
    return task
  }

  cancel(tab, reason = '截图已取消，页面仍保留。') {
    for (const entry of this.pending) {
      if (entry.tab === tab) entry.controller.abort(captureError('browser_capture_cancelled', reason))
    }
  }

  async run(entry) {
    const { tab, controller } = entry, { signal } = controller
    if (signal.aborted) throw signal.reason
    const host = this.host, wc = tab.view.webContents
    if (host.shuttingDown || host.tabs.get(tab.id) !== tab || !wc || wc.isDestroyed()) throw captureError('browser_page_closed', '页面已关闭。')
    this.active = entry
    let timer
    const cancelled = new Promise((_, reject) => signal.addEventListener('abort', () => reject(signal.reason), { once: true }))
    const bounded = promise => Promise.race([promise, cancelled])
    const wait = () => bounded(new Promise(resolve => setTimeout(resolve, 40)))
    const onGone = () => controller.abort(captureError('browser_page_closed', '截图时页面已退出。'))
    wc.once('destroyed', onGone)
    const throttling = wc.backgroundThrottling
    const originalBounds = tab.view.getBounds()
    let moved = false
    try {
      timer = setTimeout(() => controller.abort(captureError('browser_capture_timeout', '页面画面暂时无法读取，请稍后重试。')), 8000)
      wc.backgroundThrottling = false
      // Keep the user's selected page in place. Background/hidden pages acquire
      // a temporary surface that cannot take focus, mouse input or a taskbar slot.
      const visible = host.active === tab.id && host.bounds && tab.view.getVisible() && host.window.isVisible() && !host.window.isMinimized()
      if (!visible) {
        if (!this.window || this.window.isDestroyed()) {
          this.window = new BrowserWindow({ show: false, focusable: false, skipTaskbar: true, frame: false,
            webPreferences: { nodeIntegration: false, contextIsolation: true, sandbox: true } })
          this.window.setOpacity(0)
          this.window.setIgnoreMouseEvents(true)
        }
        const bounds = tab.view.getBounds()
        const width = Math.max(1, bounds.width || 1280), height = Math.max(1, bounds.height || 720)
        this.window.setContentSize(width, height)
        host.window.contentView.removeChildView(tab.view)
        this.window.contentView.addChildView(tab.view)
        moved = true
        tab.view.setBounds({ x: 0, y: 0, width, height })
        tab.view.setVisible(true)
        this.window.showInactive()
      }
      // Reattaching a view can leave the previous compositor frame available
      // while its DOM has already changed. Allow layout and a new frame before
      // capturePage; a non-empty image alone does not prove current pixels.
      wc.invalidate()
      await bounded(wc.executeJavaScriptInIsolatedWorld(42, [{ code:
        'new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))' }]))
      // Loading has a separate lifecycle from painting. A newly attached view
      // may not yet have a surface even after did-finish-load.
      let picture
      while (!picture) {
        if (signal.aborted) throw signal.reason
        try {
          const next = await bounded(wc.capturePage(undefined, { stayHidden: false, stayAwake: true }))
          if (!next.isEmpty()) picture = next
        } catch (error) {
          if (signal.aborted) throw signal.reason
          if (!/display surface not available|UnknownVizError/.test(String(error))) throw error
        }
        if (!picture) await wait()
      }
      return picture.toPNG().toString('base64')
    } finally {
      clearTimeout(timer)
      if (!wc.isDestroyed()) { wc.removeListener('destroyed', onGone); wc.backgroundThrottling = throttling }
      if (moved && this.window && !this.window.isDestroyed()) {
        this.window.contentView.removeChildView(tab.view)
        this.window.hide()
        if (!wc.isDestroyed() && host.tabs.get(tab.id) === tab && !host.shuttingDown && !host.window.isDestroyed()) {
          host.window.contentView.addChildView(tab.view)
          tab.view.setBounds(originalBounds)
        }
      }
      this.active = null
      if (!host.shuttingDown) host.present()
    }
  }

  async shutdown() {
    this.closed = true
    for (const entry of this.pending) this.cancel(entry.tab)
    await this.tail
    if (this.window && !this.window.isDestroyed()) this.window.destroy()
  }
}

import { ipcMain, shell } from 'electron'

// Only the workbench's own main frame receives desktop commands. Guest pages
// can relinquish input ownership through their preload and have no bridge API.
export function connectWorkbench(window, host) {
  const trusted = event => {
    if (event.sender !== window.webContents || event.senderFrame !== window.webContents.mainFrame) {
      throw new Error('此页面没有桌面接口。')
    }
  }
  ipcMain.handle('browser:state', event => { trusted(event); return host.state() })
  ipcMain.on('browser:human-input', event => host.humanInput(event.sender))
  ipcMain.handle('browser:command', async (event, request) => {
    trusted(event)
    const { action, session_id, id } = request
    if (action === 'reveal-download') {
      const download = host.downloads.find(item => item.id === id && item.session_id === session_id)
      if (!download) throw new Error('当前对话里找不到这份下载记录。')
      shell.showItemInFolder(download.path)
      return
    }
    if (action === 'layout') { host.layout(request.bounds, session_id); return }
    if (action === 'create') { const tab = host.create(session_id, request.url); return host.summary(tab) }
    if (action === 'select') { host.get(session_id, id); host.select(id); return }
    if (action === 'control') return host.transfer(session_id, id, request.mode)
    if (action === 'close') { host.close(session_id, id); return }
    return host.action(session_id, id, request, { human: true })
  })
  host.on('changed', state => {
    if (!window.webContents.isDestroyed()) window.webContents.send('browser:changed', state)
  })
  window.webContents.on('will-navigate', event => event.preventDefault())
  window.webContents.on('did-start-loading', () => host.layout(null))
  window.webContents.setWindowOpenHandler(() => ({ action: 'deny' }))
  window.on('resize', () => {
    if (!window.webContents.isDestroyed()) window.webContents.send('browser:layout')
  })
  window.webContents.on('zoom-changed', () => window.webContents.send('browser:layout'))
}

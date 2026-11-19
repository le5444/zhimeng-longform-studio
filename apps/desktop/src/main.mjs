import { app, BrowserWindow } from 'electron'
import { fileURLToPath } from 'node:url'
import path from 'node:path'
import { BrowserHost } from './browser-host.mjs'
import { startBridge } from './bridge.mjs'
import { connectWorkbench } from './workbench-bridge.mjs'

const source=path.dirname(fileURLToPath(import.meta.url))
const directory=path.resolve(process.env.ZHIMENG_BROWSER_DATA||path.join(app.getPath('appData'),'织梦','browser'))
const lab=process.argv.includes('--lab')
const workbenchUrl=new URL(process.env.ZHIMENG_WORKBENCH_URL||'http://127.0.0.1:5173')
if(!['127.0.0.1','localhost','[::1]'].includes(workbenchUrl.hostname)||workbenchUrl.protocol!=='http:')throw new Error('工作台需要本机 HTTP 地址。')
app.setPath('userData',directory)
app.setPath('sessionData',directory)
if(!app.requestSingleInstanceLock()){app.quit()} else { void (async()=>{
  await app.whenReady()
  const window=new BrowserWindow({title:lab?'织梦 · 共享浏览器验证':'织梦',show:false,width:1440,height:900,minWidth:600,minHeight:400,
    webPreferences:{preload:path.join(source,'preload.cjs'),nodeIntegration:false,contextIsolation:true,sandbox:true}})
  window.setMenuBarVisibility(false)
  window.once('ready-to-show',()=>window.show())
  const host=new BrowserHost(window,directory)
  let stopBridge
  connectWorkbench(window,host)
  app.on('second-instance',()=>{if(window.isMinimized())window.restore();window.focus()})
  await host.restore()
  stopBridge=await startBridge(host,directory)
  if(lab)await window.loadFile(path.join(source,'shell.html'))
  else await window.loadURL(workbenchUrl.href)
  let quitting=false
  app.on('before-quit',event=>{
    if(quitting)return
    event.preventDefault();quitting=true
    void (async()=>{await host.shutdown();await stopBridge();app.quit()})()
  })
  app.on('window-all-closed',()=>app.quit())
})().catch(error=>{console.error(error);app.quit()}) }

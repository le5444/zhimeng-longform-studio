import { WebContentsView, session, dialog } from 'electron'
import { EventEmitter } from 'node:events'
import { createHash, randomUUID } from 'node:crypto'
import { createReadStream } from 'node:fs'
import { mkdir, readFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { BrowserConflict, PageControl, pageUrl } from './browser-state.mjs'
import { observationScript, actionScript } from './page-scripts.mjs'
import { ActionLedger, atomicJson } from './action-ledger.mjs'
import { PageCapture } from './page-capture.mjs'

export class BrowserHost extends EventEmitter {
  constructor(window, directory) {
    super(); this.window=window; this.directory=directory; this.tabs=new Map(); this.downloads=[]
    this.active=null; this.bounds=null; this.saving=Promise.resolve(); this.error=null
    this.ledger=new ActionLedger(directory); this.restoring=false; this.presentedSession=null
    this.capture=new PageCapture(this)
    this.profile=session.fromPartition('persist:zhimeng-browser')
    this.profile.on('will-download', (_event,item,contents)=>void this.download(item,contents))
    this.grantedPermissions=new Set()
    this.profile.setPermissionCheckHandler((_wc,permission,origin)=>this.grantedPermissions.has(origin+'|'+permission))
    this.profile.setPermissionRequestHandler(async(contents,permission,callback,details)=>{
      const origin=details.requestingUrl||contents.getURL()
      const {response}=await dialog.showMessageBox(this.window,{type:'question',message:`允许 ${origin} 使用 ${permission}？`,buttons:['不允许','允许本次浏览器会话'],defaultId:0,cancelId:0})
      if(response===1)this.grantedPermissions.add(new URL(origin).origin+'|'+permission)
      callback(response===1)
    })
  }
  async restore() {
    await mkdir(this.directory,{recursive:true})
    await mkdir(path.join(this.directory,'downloads'),{recursive:true})
    await this.ledger.restore()
    this.restoring=true
    try {
      const saved=JSON.parse(await readFile(path.join(this.directory,'browser.json'),'utf8'))
      this.downloads=saved.downloads.map(d=>d.status==='progressing'?{...d,status:'interrupted'}:d)
      for(const tab of saved.tabs) this.create(tab.session_id,tab.url,{id:tab.id,restored:true})
    } catch(error) { if(error.code!=='ENOENT') {console.error('Browser restore failed:',error);this.error='上次浏览器记录无法读取，原文件已保留。';this.readOnlyState=true} }
    this.restoring=false;this.changed()
  }
  summary(tab) {
    const wc=tab.view.webContents
    return {id:tab.id,session_id:tab.sessionId,url:wc.getURL()||tab.url,title:wc.getTitle()||'新页面',
      mode:tab.control.mode,generation:tab.control.generation,loading:wc.isLoading(),error:tab.error,restored:tab.restored,
      can_back:wc.navigationHistory.canGoBack(),can_forward:wc.navigationHistory.canGoForward()}
  }
  state(sessionId) { return {tabs:[...this.tabs.values()].filter(t=>!sessionId||t.sessionId===sessionId).map(t=>this.summary(t)),
    active:this.active,downloads:this.downloads.filter(d=>!sessionId||d.session_id===sessionId),error:this.error} }
  changed() {
    if(this.shuttingDown||this.window.isDestroyed()) return
    if(this.restoring)return
    this.emit('changed',this.state())
    if(this.readOnlyState) return
    const saved={tabs:[...this.tabs.values()].map(t=>({id:t.id,session_id:t.sessionId,url:t.view.webContents.getURL()||t.url})),downloads:structuredClone(this.downloads)}
    this.saving=this.saving.then(async()=>{
      await atomicJson(path.join(this.directory,'browser.json'),saved)
    }).catch(()=>{this.error='浏览器状态保存失败，当前页面仍保留。';this.emit('changed',this.state())})
  }
  create(sessionId,url='about:blank',{id=randomUUID(),restored=false,options={},load=true}={}) {
    if(typeof sessionId!=='string'||!sessionId.trim()) throw new TypeError('页面需要所属对话。')
    url=pageUrl(url)
    const view=new WebContentsView({...options.webContents?{webContents:options.webContents}:{},webPreferences:{...options.webPreferences,session:this.profile,nodeIntegration:false,contextIsolation:true,sandbox:true,preload:fileURLToPath(new URL('./page-input.cjs',import.meta.url))}})
    const tab={id,sessionId,url,view,control:new PageControl(),error:null,restored,tail:Promise.resolve()}
    this.tabs.set(id,tab); const wc=view.webContents
    view.setBounds({x:0,y:0,width:1280,height:720});view.setVisible(false)
    wc.on('did-start-navigation',(_e,_url,_inPlace,isMainFrame)=>{if(isMainFrame){tab.control.invalidate();tab.error=null;this.changed()}})
    wc.on('did-navigate-in-page',()=>{tab.control.invalidate();this.changed()})
    wc.on('did-stop-loading',()=>this.changed())
    wc.on('page-title-updated',()=>this.changed())
    wc.on('did-fail-load',(_e,code,message,_url,isMainFrame)=>{if(isMainFrame&&code!==-3){tab.error=message;this.changed()}})
    wc.on('render-process-gone',(_e,details)=>{tab.control.transfer('user');tab.error='页面进程已退出：'+details.reason;this.changed()})
    // Direct human interaction takes ownership; DOM actions from world 42 don't synthesize native input.
    const human=()=>{if(!tab.injectingInput){this.capture.cancel(tab);if(tab.control.mode!=='user'){tab.control.transfer('user');this.changed()}}}
    tab.human=human
    wc.on('before-input-event',human)
    wc.on('before-mouse-event',(_e,event)=>{if(event.type==='mouseDown'||event.type==='mouseWheel') human()})
    wc.setWindowOpenHandler(details=>{
      try{pageUrl(details.url)}catch(error){tab.error=error.message;this.changed();return {action:'deny'}}
      return {action:'allow',createWindow:options=>{
        const child=this.create(sessionId,'about:blank',{options,load:false});child.control.transfer(tab.control.mode);this.select(child.id);return child.view.webContents
      }}
    })
    this.select(id)
    if(load) void wc.loadURL(url).catch(error=>{tab.error=error.message;this.changed()})
    this.changed(); return tab
  }
  get(sessionId,id) {
    const tab=this.tabs.get(id)
    if(!tab||tab.sessionId!==sessionId) {const error=new Error('当前对话里找不到这个页面。');error.status=404;throw error}
    return tab
  }
  select(id) {
    this.active=id
    this.present()
    this.changed()
  }
  present() {
    if(this.window.isDestroyed())return
    for(const tab of this.tabs.values()){
      if(this.capture.active?.tab===tab)continue
      this.window.contentView.addChildView(tab.view)
      const visible=tab.id===this.active&&this.bounds&&(!this.presentedSession||tab.sessionId===this.presentedSession)
      tab.view.setVisible(Boolean(visible))
      if(visible)tab.view.setBounds(this.bounds)
    }
  }
  layout(bounds,sessionId=null) {
    this.presentedSession=sessionId
    if(!bounds){this.bounds=null;this.present();return}
    const zoom=this.window.webContents.getZoomFactor()
    bounds=Object.fromEntries(Object.entries(bounds).map(([key,value])=>[key,value*zoom]))
    const size=this.window.getContentSize()
    const next={x:Math.max(0,Math.round(bounds.x)),y:Math.max(0,Math.round(bounds.y)),
      width:Math.max(0,Math.min(size[0]-Math.max(0,Math.round(bounds.x)),Math.round(bounds.width))),
      height:Math.max(0,Math.min(size[1]-Math.max(0,Math.round(bounds.y)),Math.round(bounds.height)))}
    if(Object.values(next).some(v=>!Number.isFinite(v))) throw new TypeError('无效页面尺寸。')
    this.bounds=next
    this.present()
  }
  transfer(sessionId,id,mode) {
    const tab=this.get(sessionId,id);tab.control.transfer(mode)
    if(mode==='user') {this.capture.cancel(tab);tab.view.webContents.stop()}
    this.changed();return this.summary(tab)
  }
  async observe(sessionId,id) {
    const tab=this.get(sessionId,id), identity=tab.control.observe()
    const page=await tab.view.webContents.executeJavaScriptInIsolatedWorld(42,[{code:observationScript(identity.observation_id)}])
    if(tab.control.generation!==identity.generation||tab.control.observation!==identity.observation_id) throw new BrowserConflict('stale_observation','观察期间页面发生变化，请重试。')
    return {...this.summary(tab),...identity,page}
  }
  action(sessionId,id,request,{human=false}={}) {
    const tab=this.get(sessionId,id)
    const task=tab.tail.then(async()=>{
      if(human) tab.control.transfer('user'); else tab.control.require(request.observation_id)
      const wc=tab.view.webContents, generation=tab.control.generation
      if(request.action==='navigate') await wc.loadURL(pageUrl(request.url))
      else if(request.action==='back') wc.navigationHistory.goBack()
      else if(request.action==='forward') wc.navigationHistory.goForward()
      else if(request.action==='reload') wc.reload()
      else if(request.action==='close'){this.close(sessionId,id);return {closed:true,tab_id:id}}
      else if(request.action==='press') {
        if(typeof request.key!=='string'||!request.key)throw new TypeError('按键需要 key。')
        wc.focus()
        tab.injectingInput=true
        try{
          wc.sendInputEvent({type:'keyDown',keyCode:request.key})
          // Windows activation/text insertion also needs the character event.
          const character=request.key==='Enter'?'\r':request.key==='Space'?' ':request.key.length===1?request.key:null
          if(character)wc.sendInputEvent({type:'char',keyCode:character})
          wc.sendInputEvent({type:'keyUp',keyCode:request.key})
        }
        finally{tab.injectingInput=false}
      }
      else if(['click','fill','scroll'].includes(request.action)) await wc.executeJavaScriptInIsolatedWorld(42,[{code:actionScript(request)}],true)
      else throw new TypeError('未知浏览器动作。')
      tab.control.observation=null;this.changed()
      return {tab:this.summary(tab),control_changed:tab.control.generation!==generation}
    })
    tab.tail=task.catch(()=>{});return task
  }
  async screenshot(sessionId,id) {
    return this.capture.capture(this.get(sessionId,id))
  }
  close(sessionId,id) {
    const tab=this.get(sessionId,id);this.capture.cancel(tab);tab.control.invalidate();this.window.contentView.removeChildView(tab.view)
    this.tabs.delete(id);tab.view.webContents.close({waitForBeforeUnload:false})
    if(this.active===id)this.select([...this.tabs.keys()].at(-1)||null)
    this.changed()
  }
  cancel(sessionId) {
    for(const tab of this.tabs.values())if(tab.sessionId===sessionId){this.capture.cancel(tab);tab.control.transfer('user');tab.view.webContents.stop()}
    this.changed();return this.state(sessionId)
  }
  humanInput(contents) {
    const tab=[...this.tabs.values()].find(t=>t.view.webContents===contents)
    tab?.human()
  }
  async shutdown() {
    this.shuttingDown=true
    await this.capture.shutdown()
    await this.saving;await this.ledger.saving
    for(const tab of this.tabs.values())if(tab.view.webContents&&!tab.view.webContents.isDestroyed())tab.view.webContents.close({waitForBeforeUnload:false})
  }
  async download(item,contents) {
    const tab=[...this.tabs.values()].find(t=>t.view.webContents.id===contents?.id)
    if(!tab){item.cancel();return}
    // Set the path synchronously in will-download; await here would open the native save dialog.
    const name=path.basename(item.getFilename()).replace(/[<>:"/\\|?*\x00-\x1f]/g,'_')||'download'
    const target=path.join(this.directory,'downloads',randomUUID()+'-'+name)
    const saved={id:randomUUID(),session_id:tab.sessionId,tab_id:tab.id,url:item.getURL(),path:target,name,status:'progressing',bytes:0,total:item.getTotalBytes(),sha256:null}
    this.downloads.push(saved);item.setSavePath(target);this.changed()
    item.on('updated',()=>{saved.bytes=item.getReceivedBytes();saved.total=item.getTotalBytes();this.changed()})
    item.once('done',async(_event,status)=>{
      saved.status=status;saved.bytes=item.getReceivedBytes()
      if(status==='completed') {
        try{const digest=createHash('sha256');for await(const block of createReadStream(target))digest.update(block);saved.sha256=digest.digest('hex')}
        catch{saved.status='unverified'}
      }
      this.changed()
    })
  }
}

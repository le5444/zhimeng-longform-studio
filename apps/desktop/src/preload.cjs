const { contextBridge, ipcRenderer } = require('electron')
contextBridge.exposeInMainWorld('zhimengBrowser',{
  state:()=>ipcRenderer.invoke('browser:state'),
  command:request=>ipcRenderer.invoke('browser:command',request),
  onChange:callback=>{const listener=(_event,state)=>callback(state);ipcRenderer.on('browser:changed',listener);return()=>ipcRenderer.removeListener('browser:changed',listener)},
  onLayout:callback=>{const listener=()=>callback();ipcRenderer.on('browser:layout',listener);return()=>ipcRenderer.removeListener('browser:layout',listener)}
})

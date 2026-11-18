// Runs against Chromium without showing a window or touching the user's profile.
import { app, BrowserWindow, ipcMain, nativeImage } from 'electron'
import assert from 'node:assert/strict'
import { createServer } from 'node:http'
import { mkdtempSync } from 'node:fs'
import { readFile, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { BrowserHost } from '../src/browser-host.mjs'

void (async () => {
// Set both paths before yielding to Electron readiness. Otherwise its default
// session can initialize in the user's profile while mkdtemp is pending.
const directory = mkdtempSync(path.join(os.tmpdir(), 'zhimeng-browser-integration-'))
app.setPath('userData', directory)
app.setPath('sessionData', directory)
const server = createServer((_request, response) => {
  response.setHeader('Content-Type', 'text/html; charset=utf-8')
  response.end('<title>Browser integration</title><input aria-label="标题"><button id="save" onclick="document.querySelector(\'output\').textContent=document.querySelector(\'input\').value">确认</button><output></output><a target="_blank" href="/child">新页</a><div style="height:2000px">滚动</div>')
})
await new Promise(resolve => server.listen(0, '127.0.0.1', resolve))
let window, host
const delay = milliseconds => new Promise(resolve => setTimeout(resolve, milliseconds))
async function until(check, label) {
  const deadline = Date.now() + 8000
  while (Date.now() < deadline) { if (await check()) return; await delay(25) }
  throw new Error('Timed out: ' + label)
}
const steps = []
const watchdog = setTimeout(() => { console.error(JSON.stringify({ timeout: true, steps, directory })); app.exit(1) }, 30000)
try {
  await app.whenReady()
  assert.equal(app.getPath('sessionData'), directory)
  window = new BrowserWindow({ show: false, width: 900, height: 700, webPreferences: { offscreen: true } })
  await window.loadURL('about:blank')
  host = new BrowserHost(window, directory)
  ipcMain.on('browser:human-input', event => host.humanInput(event.sender))
  await host.restore()
  const tab = host.create('test-session', `http://127.0.0.1:${server.address().port}/`)
  const wc = tab.view.webContents
  wc.debugger.attach('1.3')
  await wc.debugger.sendCommand('Emulation.setFocusEmulationEnabled', { enabled: true })
  host.layout({ x: 100, y: 80, width: 500, height: 400 }, 'test-session')
  await until(() => !wc.isLoading() && wc.getTitle() === 'Browser integration', 'page load')
  host.transfer('test-session', tab.id, 'agent')
  const observe = () => host.observe('test-session', tab.id)
  const action = async (kind, parameters = {}) => {
    const observed = await observe()
    return host.action('test-session', tab.id, { action: kind, observation_id: observed.observation_id, ...parameters })
  }
  await action('fill', { ref: '1', text: '真实按键验证' })
  await action('press', { key: 'Tab' })
  await delay(200)
  assert.equal(tab.control.mode, 'agent', 'Agent key must not take user control')
  assert.equal(await wc.executeJavaScript('document.activeElement.id'), 'save', 'Tab reaches the real button')
  await action('press', { key: 'Enter' })
  await until(async () => await wc.executeJavaScript('document.querySelector("output").textContent') === '真实按键验证', 'Enter confirms form')
  assert.equal(tab.control.mode, 'agent')
  steps.push('Agent Tab and Enter use real page behavior without user takeover')
  console.log(steps.at(-1))
  wc.sendInputEvent({ type: 'mouseWheel', x: 100, y: 150, deltaY: -300, deltaX: 0 })
  await until(() => tab.control.mode === 'user', 'wheel relinquishes control through guest preload')
  await until(async () => (await observe()).page.viewport.scrollY > 0, 'wheel actually scrolled')
  steps.push('Trusted wheel scrolls and transfers control to user')
  console.log(steps.at(-1))
  host.transfer('test-session', tab.id, 'agent')
  wc.sendInputEvent({ type: 'keyDown', keyCode: 'Escape' })
  wc.sendInputEvent({ type: 'keyUp', keyCode: 'Escape' })
  await until(() => tab.control.mode === 'user', 'direct native key relinquishes control')
  steps.push('Direct keyboard input still takes control')
  console.log(steps.at(-1))
  window.webContents.setZoomFactor(1.25)
  host.layout({ x: 80, y: 64, width: 400, height: 320 }, 'test-session')
  assert.deepEqual(tab.view.getBounds(), { x: 100, y: 80, width: 500, height: 400 })
  steps.push('CSS bounds follow the workbench zoom factor')
  console.log(steps.at(-1))
  host.transfer('test-session', tab.id, 'agent')
  await action('click', { ref: '3' })
  await until(() => host.tabs.size === 2 && [...host.tabs.values()][1].view.webContents.getURL().endsWith('/child'), 'popup uses the adopted contents')
  const child = [...host.tabs.values()][1]
  await until(() => !child.view.webContents.isLoading(), 'popup loads')
  assert.equal(await child.view.webContents.executeJavaScript('typeof require + ":" + typeof window.zhimengBrowser'), 'undefined:undefined')
  await host.screenshot('test-session', child.id)
  child.view.webContents.sendInputEvent({ type: 'mouseWheel', x: 100, y: 150, deltaY: -300, deltaX: 0 })
  await until(() => child.control.mode === 'user', 'popup retains guest preload')
  steps.push('Popup remains sandboxed and retains wheel takeover')
  console.log(steps.at(-1))
  await wc.executeJavaScript("document.body.style.background='#772288';document.querySelector('input').value='后台最新标题';scrollTo(0,0)")
  const beforeCapture={active:host.active,control:tab.control.mode,url:wc.getURL(),bounds:tab.view.getBounds()}
  const png = Buffer.from(await host.screenshot('test-session', tab.id), 'base64')
  assert.equal(png.subarray(0, 8).toString('hex'), '89504e470d0a1a0a')
  assert.ok(png.length > 100)
  const captured=nativeImage.createFromBuffer(png), pixels=captured.toBitmap()
  assert.ok(captured.getSize().width>100&&captured.getSize().height>100)
  const pixelOffset=(Math.floor(captured.getSize().height/2)*captured.getSize().width+Math.floor(captured.getSize().width/2))*4
  const background=[...pixels.subarray(pixelOffset,pixelOffset+3)]
  // The bottom-right pixel belongs to the native scrollbar. Sample the page
  // interior; display color conversion can round an individual channel by one.
  assert.ok(background.every((channel,index)=>Math.abs(channel-[136,34,119][index])<=1),
    'actual new background color, not old/blank pixels: '+background)
  assert.equal(host.active,beforeCapture.active)
  assert.equal(tab.control.mode,beforeCapture.control)
  assert.equal(wc.getURL(),beforeCapture.url)
  assert.deepEqual(tab.view.getBounds(),beforeCapture.bounds)
  assert.equal(await wc.executeJavaScript('document.querySelector("input").value'),'后台最新标题')
  await writeFile(path.join(directory,'background.png'),png)
  steps.push('Background screenshot contains current page pixels and preserves form, control and selected tab')
  console.log(steps.at(-1))
  const queued=[host.screenshot('test-session',tab.id),host.screenshot('test-session',tab.id)]
  host.transfer('test-session',tab.id,'user')
  const cancelled=await Promise.allSettled(queued)
  assert.ok(cancelled.every(result=>result.status==='rejected'&&result.reason.code==='browser_capture_cancelled'))
  assert.equal(host.capture.active,null)
  assert.equal(host.active,child.id)
  assert.equal(await wc.executeJavaScript('document.querySelector("input").value'),'后台最新标题')
  steps.push('User takeover cancels queued captures without moving or reloading the page')
  console.log(steps.at(-1))
  const originalExecute=wc.executeJavaScriptInIsolatedWorld.bind(wc)
  let frameEntered
  const frameStarted=new Promise(resolve=>{frameEntered=resolve})
  wc.executeJavaScriptInIsolatedWorld=(...args)=>{
    if(args[1]?.[0]?.code.includes('requestAnimationFrame')){frameEntered();return new Promise(()=>{})}
    return originalExecute(...args)
  }
  const inFlight=host.screenshot('test-session',tab.id)
  await frameStarted
  host.transfer('test-session',tab.id,'user')
  await assert.rejects(inFlight,error=>error.code==='browser_capture_cancelled')
  wc.executeJavaScriptInIsolatedWorld=originalExecute
  assert.equal(host.capture.active,null)
  assert.deepEqual(tab.view.getBounds(),beforeCapture.bounds)
  assert.equal(host.active,child.id)
  assert.equal(host.capture.window.isVisible(),false)
  assert.equal(await wc.executeJavaScript('document.querySelector("input").value'),'后台最新标题')
  assert.ok((await host.screenshot('test-session',tab.id)).length>100)
  steps.push('In-flight capture cancellation restores the view and allows another actual screenshot')
  console.log(steps.at(-1))
  await host.saving
  assert.equal(JSON.parse(await readFile(path.join(directory, 'browser.json'), 'utf8')).tabs.length, 2)
  const closing=host.screenshot('test-session',child.id)
  host.close('test-session',child.id)
  await assert.rejects(closing,error=>['browser_capture_cancelled','browser_page_closed'].includes(error.code))
  assert.equal(host.tabs.has(child.id),false)
  const finalCaptures=[host.screenshot('test-session',tab.id),host.screenshot('test-session',tab.id)]
  const outcomes=Promise.allSettled(finalCaptures)
  await host.shutdown()
  assert.ok((await outcomes).every(result=>result.status==='rejected'))
  assert.equal(host.capture.pending.size,0)
  assert.equal(host.capture.active,null)
  assert.equal(host.capture.window.isDestroyed(),true)
  steps.push('Closing a page and shutting down cancel pending capture work and dispose the temporary surface')
  console.log(steps.at(-1))
  console.log(JSON.stringify({ passed: steps.length, steps, directory }))
} catch (error) {
  console.error(JSON.stringify({ completed: steps, directory }))
  console.error(error)
  process.exitCode = 1
} finally {
  clearTimeout(watchdog)
  if (host) await host.shutdown()
  if (window && !window.isDestroyed()) window.destroy()
  server.close()
  app.exit(process.exitCode || 0)
}
})().catch(error => { console.error(error); app.exit(1) })

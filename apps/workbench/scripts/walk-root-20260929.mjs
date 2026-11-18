// 2026-09-29 回炉重造后新界面的真机走查（隔离实例 5175 → 8788，真模型）。
// 先把书的副本复制成一个新名字的文件夹（中转站对一字不差的请求会卡，见 AGENTS.md），
// 从左栏「添加项目 → 打开已有文件夹」打开它，然后按 STEPS 一句一句发；每秒采一次样，记下多久出字、步骤、修改卡、失败卡、记忆条。
// 用法：node scripts/walk-root-20260929.mjs            （STEPS 在下面，改了就换话）
//       WALK_BOOK=<书的副本> node scripts/walk-root-20260929.mjs
import { chromium } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const repo = path.join(here, '..', '..', '..')
const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 12)
const walkDir = path.join(repo, '.runtime', 'e2e-walk-20260929', stamp)
const outDir = path.join(walkDir, 'shots')
fs.mkdirSync(outDir, { recursive: true })
const base = process.env.SHOT_BASE ?? 'http://127.0.0.1:5175'
if (base.includes('5173') || base.includes('8787')) throw new Error('走查只许对隔离实例跑（5175 → 8788）')

const source = process.env.WALK_BOOK ?? path.join(repo, '.runtime', 'e2e-walk-20260926', 'books', '宗门只剩七个人，还全是江湖骗子')
if (!fs.existsSync(source)) throw new Error(`没有书的副本：${source}`)
const bookRoot = path.join(repo, '.runtime', 'e2e-walk-20260929', 'books')
const book = path.join(bookRoot, `新界面走查-${stamp}`)
fs.mkdirSync(bookRoot, { recursive: true })
fs.cpSync(source, book, { recursive: true })

const STEPS = [
  { key: 'edit4', text: '把第 4 章开头第一段改得更有画面感，只改这一段，改完直接存。' },
  { key: 'undo', text: '刚才那次修改撤回。' },
  { key: 'remember', newChat: true, text: '记住：我写东西喜欢短句，别用太多形容词。另外我这周在赶第 6 章。' },
]
const RUN_TIMEOUT_MS = 15 * 60 * 1000

const log = (...parts) => console.log(new Date().toLocaleTimeString('zh-CN', { hour12: false }), ...parts)
const report = { started_at: new Date().toISOString(), book, steps: [], problems: [] }
const saveReport = () => fs.writeFileSync(path.join(walkDir, 'walk-report.json'), JSON.stringify(report, null, 2), 'utf8')

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 } })
page.on('pageerror', (e) => report.problems.push(`pageerror: ${e.message}`))
page.on('console', (m) => { if (m.type() === 'error') report.problems.push(`console: ${m.text().slice(0, 300)}`) })
page.on('response', (r) => { if (r.status() >= 400) report.problems.push(`http ${r.status()} ${r.request().method()} ${r.url()}`) })

let shotNo = 0
async function shot(name) {
  shotNo += 1
  const file = `${String(shotNo).padStart(2, '0')}-${name}.png`
  await page.screenshot({ path: path.join(outDir, file) })
  return file
}
async function texts(selector) {
  return page.$$eval(selector, (nodes) => nodes
    .filter((n) => n.offsetParent !== null)
    .map((n) => (n.textContent ?? '').trim().replace(/\s+/g, ' '))
    .filter(Boolean))
}
const busy = async () => (await page.locator('.zm-send.is-stop').count()) > 0

try {
  await page.goto(`${base}/`)
  await page.waitForSelector('.zm-side', { timeout: 60000 })
  await page.waitForTimeout(2000)

  // 打开书的副本：左栏「添加项目 → 打开已有文件夹」，地址栏粘路径，「打开这个文件夹」
  await page.getByRole('button', { name: '添加项目' }).click()
  await page.getByRole('menuitem', { name: /打开已有文件夹/ }).click()
  const address = page.getByRole('textbox', { name: '位置' })
  await address.fill(book)
  await address.press('Enter')
  await page.waitForTimeout(2000)
  await page.getByRole('button', { name: '打开这个文件夹' }).click()
  const openedAt = Date.now()
  await page.waitForSelector('textarea.zm-composer-input', { timeout: 120000 })
  await page.waitForFunction(() => document.querySelector('.zm-chat-title p')?.textContent?.startsWith('在：'), null, { timeout: 120000 })
  report.open_folder_s = Math.round((Date.now() - openedAt) / 1000)
  report.after_open_head = await texts('.zm-chat-head')
  log('folder opened in', report.open_folder_s, 's; shot', await shot('folder-open'))
  saveReport()

  for (const step of STEPS) {
    const record = { key: step.key, text: step.text, samples: [], shots: [] }
    report.steps.push(record)
    if (step.newChat) {
      await page.getByRole('button', { name: '新对话' }).first().click()
      await page.waitForTimeout(2500)
    }
    const textarea = page.locator('textarea.zm-composer-input').first()
    await textarea.click()
    await textarea.fill(step.text)
    await page.keyboard.press('Enter')
    const sentAt = Date.now()
    log('sent:', step.text)

    let sawBusy = false
    let idlePolls = 0
    while (Date.now() - sentAt < RUN_TIMEOUT_MS) {
      await page.waitForTimeout(Date.now() - sentAt < 60000 ? 1000 : 4000)
      const isBusy = await busy()
      if (isBusy) sawBusy = true
      const live = (await texts('.zm-msg.is-live')).join(' | ')
      const status = (await texts('.zm-status')).join(' | ')
      const failure = await texts('.zm-failure')
      const t = Math.round((Date.now() - sentAt) / 1000)
      record.samples.push({ t, busy: isBusy, status: status.slice(0, 80), live_len: live.length, failure: failure.length })
      if (record.first_live_text_s === undefined && live.length > 20) record.first_live_text_s = t
      if (record.samples.length % 15 === 1) record.shots.push(await shot(`${step.key}-t${t}`))
      if (record.samples.length % 10 === 0) {
        log(`[${step.key}] t=${t}s busy=${isBusy} live=${live.length} status=${status.slice(0, 50)}`)
        saveReport()
      }
      if (!isBusy && (sawBusy || Date.now() - sentAt > 30000)) {
        idlePolls += 1
        if (idlePolls >= 2) break
      } else {
        idlePolls = 0
      }
    }
    record.elapsed_s = Math.round((Date.now() - sentAt) / 1000)
    record.saw_busy = sawBusy
    // 自动记忆是答完以后后台另问一次：记忆那一步多等一会儿看「记住了」
    if (step.key === 'remember') {
      for (let i = 0; i < 20 && (await page.locator('.zm-memory-note').count()) === 0; i += 1) await page.waitForTimeout(5000)
      record.memory_note = await texts('.zm-memory-note')
    }
    const agents = await texts('.zm-msg.is-agent:not(.is-live)')
    record.last_agent_message = agents.slice(-1)[0] ?? ''
    record.step_folds = await texts('.zm-msg.is-agent .zm-fold-head')
    record.cards = await texts('.zm-change')
    record.failure = await texts('.zm-failure')
    record.toasts = await texts('.zm-toast')
    record.final_shot = await shot(`${step.key}-done`)
    log(`[${step.key}] done in ${record.elapsed_s}s; first text at ${record.first_live_text_s}s; cards:`, record.cards.map((c) => c.slice(0, 80)), 'failure:', record.failure.map((c) => c.slice(0, 80)))
    saveReport()
    await page.waitForTimeout(8000) // 中转站按账户限并发：两句之间歇一会儿
  }
} catch (error) {
  report.problems.push(`script: ${error?.stack ?? error}`)
  try { await shot('error') } catch { /* 拍不了就算了 */ }
} finally {
  report.finished_at = new Date().toISOString()
  saveReport()
  await browser.close()
  log('report:', path.join(walkDir, 'walk-report.json'))
  log('problems:', [...new Set(report.problems)].slice(0, 20))
}

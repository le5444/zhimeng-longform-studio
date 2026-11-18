// 9-29 回炉重造的新界面（src/app/Root.tsx）截图：对着隔离实例（5175 → 8788）拍首页、项目对话、右栏、设置每一节、窄窗口、暗色。
// 只读：只点导航、页签、打开文件和设置页，不点保存 / 发送 / 删除 / 开关。
// 用法：node scripts/shot-root-20260929.mjs [步骤名…]（不给就全拍）；SHOT_OUT 换输出目录，SHOT_PROJECT 换打开哪个项目（左栏名字里的字）。
import { chromium } from '@playwright/test'
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const here = path.dirname(fileURLToPath(import.meta.url))
const outDir = process.env.SHOT_OUT ?? path.join(here, '..', '..', '..', '.runtime', 'frontend-rebuild-20260929', 'shots-root')
fs.mkdirSync(outDir, { recursive: true })
const base = process.env.SHOT_BASE ?? 'http://127.0.0.1:5175'
if (base.includes('5173') || base.includes('8787')) throw new Error('只许对隔离实例跑（5175 → 8788）：5173 是作者正在用的那一套')
const home = `${base}/`
const projectName = process.env.SHOT_PROJECT ?? '续写二-宗门'
const wanted = new Set(process.argv.slice(2))

const browser = await chromium.launch({ channel: 'chrome', headless: true })
const page = await browser.newPage({ viewport: { width: 1440, height: 900 } })
const problems = []
page.on('pageerror', (error) => problems.push(`pageerror: ${error.message}`))
page.on('console', (message) => { if (message.type() === 'error') problems.push(`console: ${message.text().slice(0, 300)}`) })
page.on('response', (response) => { if (response.status() >= 400) problems.push(`http ${response.status()}: ${response.url()}`) })

async function shot(name) {
  await page.waitForTimeout(700)
  const size = page.viewportSize()
  const file = path.join(outDir, `${name}-${size.width}x${size.height}.png`)
  await page.screenshot({ path: file })
  console.log('shot', file)
}

async function controls(name) {
  const items = await page.$$eval('button, a, [role="tab"], [role="switch"]', (nodes) => nodes
    .filter((node) => node.offsetParent !== null)
    .map((node) => ((node.getAttribute('aria-label') || node.textContent || '').trim().replace(/\s+/g, ' ')).slice(0, 40)))
  const unique = [...new Set(items)].filter(Boolean)
  console.log(`[${name}] ${items.length} controls: ${unique.slice(0, 70).join(' | ')}`)
}

async function click(role, name, wait = 1000) {
  const target = page.getByRole(role, { name, exact: typeof name === 'string' }).first()
  if (!(await target.count())) {
    console.log(`  (没找到 ${role}「${name}」)`)
    return false
  }
  await target.click()
  await page.waitForTimeout(wait)
  return true
}

/** 页面有没有横向滚动条、有没有东西伸出窗口右边。 */
async function overflow(name) {
  const result = await page.evaluate(() => {
    const width = document.documentElement.clientWidth
    const wide = [...document.querySelectorAll('body *')]
      .filter((node) => node.getBoundingClientRect().right > width + 1 && node.offsetParent !== null)
      .slice(0, 5)
      .map((node) => `${node.tagName.toLowerCase()}.${String(node.className).split(' ')[0]}`)
    return { scroll: document.documentElement.scrollWidth > width, wide }
  })
  console.log(`[${name}] 横向滚动：${result.scroll ? '有' : '没有'}${result.wide.length ? `；伸出去的：${result.wide.join(', ')}` : ''}`)
}

const steps = {
  async home() {
    await page.goto(home)
    await page.waitForTimeout(3500)
    await shot('01-home')
    await controls('home')
    await overflow('home')
  },
  async project() {
    // 隔离库里考题建的项目很多，要找的那个常在「显示更多」下面：先把项目那一段全展开。
    const projects = page.getByRole('region', { name: '项目' })
    const more = projects.getByRole('button', { name: /显示更多/ })
    if (await more.count()) {
      await more.first().click()
      await page.waitForTimeout(600)
    }
    if (!(await click('button', new RegExp(projectName), 1500))) return
    const nested = page.locator('.zm-side-row.is-nested .zm-side-row-main').first()
    if (await nested.count()) {
      await nested.click()
      await page.waitForTimeout(3000)
    }
    await shot('02-project-chat')
    await controls('project')
    // 滚到最上面看早一点的消息和步骤
    await page.locator('.zm-thread').first().evaluate((node) => { node.scrollTop = Math.max(0, node.scrollHeight / 3) }).catch(() => undefined)
    await shot('03-project-chat-mid')
  },
  async panel() {
    if (!(await click('button', '打开右栏', 1200))) return
    await shot('04-panel-files')
    const folder = page.locator('.zm-tree-row.is-folder').first()
    if (await folder.count()) {
      await folder.click()
      await page.waitForTimeout(600)
    }
    const file = page.locator('.zm-tree-row.is-file', { hasText: /\.md$/ }).first()
    if (await file.count()) {
      await file.click()
      await page.waitForTimeout(1500)
      await shot('05-panel-file')
    }
    if (await click('tab', '修改', 1500)) await shot('06-panel-changes')
    if (await click('button', '右栏放大，对话让到一边', 1000)) {
      await shot('07-panel-wide')
      await click('button', '右栏还原成窄的', 600)
    }
    await click('button', '收起右栏', 600)
  },
  async settings() {
    if (!(await click('button', '设置', 1500))) return
    await shot('08-settings-models')
    await controls('settings')
    for (const label of ['外观', '记忆', '技能库', '关于']) {
      if (await click('button', label, 1500)) {
        await shot(`09-settings-${label}`)
        await overflow(`settings-${label}`)
      }
    }
    await click('button', '关上设置', 800)
  },
  async folder() {
    if (!(await click('button', '添加项目', 600))) return
    if (!(await click('menuitem', /打开已有文件夹/, 1500))) return
    await shot('10-folder-picker')
    await click('button', '取消', 800)
  },
  async dark() {
    // 主题记在这个无头浏览器自己的本机存储里：拍前改成「跟随系统」再让系统是暗色，拍完改回来。
    await page.goto(home)
    const before = await page.evaluate(() => window.localStorage.getItem('zhimeng.desk.theme'))
    await page.evaluate(() => window.localStorage.setItem('zhimeng.desk.theme', 'system'))
    await page.emulateMedia({ colorScheme: 'dark' })
    await page.reload()
    await page.waitForTimeout(3000)
    await shot('11-dark-home')
    if (await click('button', '设置', 1500)) {
      await shot('11-dark-settings')
      await click('button', '关上设置', 600)
    }
    await page.emulateMedia({ colorScheme: 'light' })
    await page.evaluate((value) => { if (value === null) window.localStorage.removeItem('zhimeng.desk.theme'); else window.localStorage.setItem('zhimeng.desk.theme', value) }, before)
  },
  async narrow() {
    await page.setViewportSize({ width: 820, height: 900 })
    await page.goto(home)
    await page.waitForTimeout(3000)
    await shot('12-narrow-chat')
    await overflow('narrow-chat')
    if (await click('button', '对话列表', 800)) await shot('13-narrow-nav')
    await page.setViewportSize({ width: 390, height: 844 })
    await click('button', 'Agent', 800)
    await shot('14-phone-chat')
    await overflow('phone-chat')
  },
}

const order = Object.keys(steps).filter((name) => wanted.size === 0 || wanted.has(name))
for (const name of order) {
  console.log(`== ${name}`)
  await steps[name]()
}
console.log(problems.length ? `problems:\n${[...new Set(problems)].slice(0, 40).join('\n')}` : 'problems: none')
await browser.close()

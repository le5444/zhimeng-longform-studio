// 删掉界面文字表（src/app/strings.ts 的 T）里没人用的键：9-29 回炉重造后旧外壳的字大半没人用了。
// 怎么算「有人用」：src 下别的文件（含测试）里出现 T.a.b.c；别名也算（const E = T.agent.events 之后的 E.x）；
// 整组被当成值用（T.settings.themes 传出去、T.changes.status[x] 按变量取）就整组留着。
// 用法（在 apps/workbench 下）：node scripts/prune-strings.mjs        先只列要删的
//                              node scripts/prune-strings.mjs --write  真删（删完跑 npm run typecheck）
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const here = path.dirname(fileURLToPath(import.meta.url))
const src = path.join(here, '..', 'src')
const target = path.join(src, 'app', 'strings.ts')
const write = process.argv.includes('--write')

function walk(dir, out = []) {
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name)
    if (entry.isDirectory()) walk(full, out)
    else if (/\.(ts|tsx)$/.test(entry.name) && full !== target) out.push(full)
  }
  return out
}

// 用到的文字：别名换回全名，好统一查
let usage = ''
for (const file of walk(src)) {
  let text = fs.readFileSync(file, 'utf8')
  if (!/\bT\b/.test(text)) continue
  for (const match of text.matchAll(/(?:const|let)\s+([A-Z][A-Za-z0-9]*)\s*=\s*T\.([A-Za-z0-9_.]+)/g)) {
    const [, alias, full] = match
    text = text.replace(new RegExp(`\\b${alias}(?=[.[])`, 'g'), `T.${full}`)
  }
  usage += `\n${text}`
}

const sourceText = fs.readFileSync(target, 'utf8')
const source = ts.createSourceFile(target, sourceText, ts.ScriptTarget.Latest, true)
let root = null
source.forEachChild((node) => {
  if (ts.isVariableStatement(node)) {
    for (const decl of node.declarationList.declarations) {
      const init = decl.initializer && ts.isAsExpression(decl.initializer) ? decl.initializer.expression : decl.initializer
      if (decl.name.getText(source) === 'T' && init && ts.isObjectLiteralExpression(init)) root = init
    }
  }
})
if (!root) throw new Error('strings.ts 里没找到 export const T = { … }')

const escape = (text) => text.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
/** 这一条（或它下面整组）有没有人用。 */
function usedAsWhole(pathText) {
  // T.a.b 后面紧跟的不是「.名字」：整组当值用了（传出去、按变量取、解构）
  return new RegExp(`\\bT\\.${escape(pathText)}(?![A-Za-z0-9_.])`).test(usage)
}
function usedLeaf(pathText) {
  return new RegExp(`\\bT\\.${escape(pathText)}(?![A-Za-z0-9_])`).test(usage)
}

const removals = []
let kept = 0
function visit(object, prefix) {
  let keptHere = 0
  for (const property of object.properties) {
    if (!ts.isPropertyAssignment(property) && !ts.isShorthandPropertyAssignment(property)) { keptHere += 1; continue }
    const name = property.name.getText(source).replace(/^['"]|['"]$/g, '')
    const full = prefix ? `${prefix}.${name}` : name
    const init = ts.isPropertyAssignment(property) ? property.initializer : null
    const inner = init && ts.isAsExpression(init) ? init.expression : init
    if (inner && ts.isObjectLiteralExpression(inner) && !usedAsWhole(full)) {
      const keptInside = visit(inner, full)
      if (keptInside === 0) removals.push({ property, full })
      else keptHere += 1
      continue
    }
    if (usedLeaf(full) || usedAsWhole(full)) {
      keptHere += 1
      kept += 1
    } else {
      removals.push({ property, full })
    }
  }
  return keptHere
}
visit(root, '')

// 同一段里嵌套的只删最外层
const outer = removals.filter((item) => !removals.some((other) => other !== item && other.property.pos <= item.property.pos && item.property.end <= other.property.end))
console.log(`留 ${kept} 条；删 ${outer.length} 处（${removals.length} 条）`)
for (const item of outer.slice(0, 400)) console.log('  -', item.full)

if (write) {
  // 从后往前删：整行删掉（含行首缩进、行尾逗号和紧挨着的上方注释行）
  let text = sourceText
  for (const { property } of [...outer].sort((a, b) => b.property.getStart(source) - a.property.getStart(source))) {
    let start = property.getFullStart()
    let end = property.getEnd()
    if (text[end] === ',') end += 1
    // 吃掉到行尾的空白和换行
    while (end < text.length && (text[end] === ' ' || text[end] === '\t')) end += 1
    if (text[end] === '\r') end += 1
    if (text[end] === '\n') end += 1
    // 前面的换行留一个
    while (start < property.getStart(source) && /\s/.test(text[start]) && text[start] !== '\n') start += 1
    if (text[start] === '\n') start += 1
    text = text.slice(0, start) + text.slice(end)
  }
  const tmp = `${target}.tmp`
  fs.writeFileSync(tmp, text)
  fs.renameSync(tmp, target)
  console.log(`写回 ${target}：${sourceText.length} → ${text.length} 字符`)
}

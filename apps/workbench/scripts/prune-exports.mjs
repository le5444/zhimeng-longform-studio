// 按名单删掉前端里没人用的顶层导出，再反复删「因此变成没人用」的本地函数、类型和 import，直到类型检查不再报「声明了但没用」。
// 用法（在 apps/workbench 下）：node scripts/prune-exports.mjs <名单.json>
//   名单格式：{ "api/changeSets.ts": ["getReaderReview", ...], ... }（路径相对 src/）
// 只动名单里的文件和类型检查点名的文件；保留原换行风格；不碰测试文件里的用例（测试报错另行处理）。
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const src = path.join(root, 'src')
const plan = JSON.parse(fs.readFileSync(process.argv[2], 'utf8'))

function declaredNames(statement) {
  if (ts.isVariableStatement(statement)) {
    return statement.declarationList.declarations.map((d) => (ts.isIdentifier(d.name) ? d.name.text : null)).filter(Boolean)
  }
  if (
    (ts.isFunctionDeclaration(statement) || ts.isClassDeclaration(statement) || ts.isInterfaceDeclaration(statement)
      || ts.isTypeAliasDeclaration(statement) || ts.isEnumDeclaration(statement)) && statement.name
  ) {
    return [statement.name.text]
  }
  return []
}

function isExported(statement) {
  return (ts.getCombinedModifierFlags(statement) & ts.ModifierFlags.Export) !== 0
}

function parse(file, text) {
  return ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
}

function removeRanges(text, ranges) {
  ranges.sort((a, b) => b[0] - a[0])
  let out = text
  for (const [start, end] of ranges) out = out.slice(0, start) + out.slice(end)
  const eol = text.includes('\r\n') ? '\r\n' : '\n'
  const blank = eol === '\r\n' ? /(\r\n){3,}/g : /\n{3,}/g
  return out.replace(blank, eol + eol)
}

/** 删掉文件里名字在 names 里的顶层声明（exportedOnly=true 时只删 export 的）。返回删了几个。 */
function pruneFile(file, names, { exportedOnly }) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = parse(file, text)
  const ranges = []
  for (const statement of sf.statements) {
    const declared = declaredNames(statement)
    if (!declared.length || !declared.every((name) => names.has(name))) continue
    if (exportedOnly && !isExported(statement)) continue
    ranges.push([statement.getFullStart(), statement.getEnd()])
  }
  if (ranges.length) fs.writeFileSync(file, removeRanges(text, ranges), 'utf8')
  return ranges.length
}

/** 删掉 import 里没用的名字；整条 import 都没用就整条删。 */
function pruneImports(file, unused) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = parse(file, text)
  const ranges = []
  const edits = []
  for (const statement of sf.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue
    const clause = statement.importClause
    const named = clause.namedBindings && ts.isNamedImports(clause.namedBindings) ? clause.namedBindings.elements : []
    const defaultUnused = clause.name ? unused.has(clause.name.text) : true
    const keep = named.filter((element) => !unused.has(element.name.text))
    const namespaceUnused = clause.namedBindings && ts.isNamespaceImport(clause.namedBindings) ? unused.has(clause.namedBindings.name.text) : true
    const allUnused = defaultUnused && namespaceUnused && keep.length === 0
    if (allUnused && (clause.name || clause.namedBindings)) {
      ranges.push([statement.getFullStart(), statement.getEnd()])
    } else if (named.length && keep.length !== named.length) {
      const bindings = clause.namedBindings
      edits.push([bindings.getStart(), bindings.getEnd(), `{ ${keep.map((element) => element.getText()).join(', ')} }`])
    }
  }
  if (!ranges.length && !edits.length) return 0
  let out = text
  const all = [...ranges.map(([s, e]) => [s, e, '']), ...edits].sort((a, b) => b[0] - a[0])
  for (const [start, end, replacement] of all) out = out.slice(0, start) + replacement + out.slice(end)
  fs.writeFileSync(file, out, 'utf8')
  return all.length
}

function unusedDiagnostics() {
  const configPath = path.join(root, 'tsconfig.app.json')
  const config = ts.getParsedCommandLineOfConfigFile(configPath, {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} })
  const program = ts.createProgram({ rootNames: config.fileNames, options: { ...config.options, noEmit: true } })
  const byFile = new Map()
  for (const diagnostic of ts.getPreEmitDiagnostics(program)) {
    if (!diagnostic.file || ![6133, 6196, 6192, 6198, 6205].includes(diagnostic.code)) continue
    const file = diagnostic.file.fileName
    if (/__tests__|\.test\.|[\\/]test[\\/]/.test(file)) continue
    const token = diagnostic.file.text.slice(diagnostic.start, diagnostic.start + diagnostic.length)
    if (!byFile.has(file)) byFile.set(file, new Set())
    byFile.get(file).add(token)
  }
  return byFile
}

let removed = 0
for (const [relative, names] of Object.entries(plan)) {
  const file = path.join(src, relative)
  const count = pruneFile(file, new Set(names), { exportedOnly: true })
  console.log(`${relative}: 删导出 ${count} / ${names.length}`)
  removed += count
}
for (let round = 1; round <= 8; round += 1) {
  const byFile = unusedDiagnostics()
  if (!byFile.size) break
  let changed = 0
  for (const [file, names] of byFile) {
    changed += pruneImports(file, names)
    changed += pruneFile(file, names, { exportedOnly: false })
  }
  console.log(`第 ${round} 轮清理没用的本地声明 / import：${changed} 处（${[...byFile.keys()].map((f) => path.relative(src, f)).join(', ')}）`)
  if (!changed) break
}
console.log(`完成：按名单删了 ${removed} 个导出`)

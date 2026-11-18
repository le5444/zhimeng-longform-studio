// 删掉只测已删函数的测试用例：读 `npm run typecheck` 的报错（TS2305/TS2724：模块里没有某个导出），
// 在对应测试文件里删掉引用这些名字的 it()/test()，删空的 describe，删 import 里的这些名字；
// 再按类型检查删掉因此没用的 import 和本地辅助函数。一条用例都不剩的测试文件整个删掉。
// 用法（在 apps/workbench 下）：npm run typecheck 2>&1 | node scripts/prune-tests.mjs
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import ts from 'typescript'

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const input = fs.readFileSync(0, 'utf8')
const missing = new Map()
for (const line of input.split(/\r?\n/)) {
  const match = line.match(/^(src[^(]+)\(\d+,\d+\): error TS(?:2305|2724): .*? exported member(?: named)? '([^']+)'/)
  if (!match) continue
  const file = path.join(root, match[1])
  if (!missing.has(file)) missing.set(file, new Set())
  missing.get(file).add(match[2])
}

const parse = (file, text) => ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true, file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)

function identifiersIn(node) {
  const names = new Set()
  const visit = (child) => {
    if (ts.isIdentifier(child)) names.add(child.text)
    ts.forEachChild(child, visit)
  }
  visit(node)
  return names
}

function calleeName(statement) {
  if (!ts.isExpressionStatement(statement) || !ts.isCallExpression(statement.expression)) return null
  let callee = statement.expression.expression
  while (ts.isPropertyAccessExpression(callee) || ts.isCallExpression(callee)) callee = callee.expression
  return ts.isIdentifier(callee) ? callee.text : null
}

/** 在一组语句里删：引用了 names 的 it/test；删完变空的 describe。返回要删的区间。 */
function collect(statements, names, ranges) {
  let kept = 0
  for (const statement of statements) {
    const callee = calleeName(statement)
    if (callee === 'it' || callee === 'test') {
      const used = identifiersIn(statement)
      if ([...names].some((name) => used.has(name))) ranges.push([statement.getFullStart(), statement.getEnd()])
      else kept += 1
    } else if (callee === 'describe') {
      const callback = statement.expression.arguments.find((arg) => ts.isArrowFunction(arg) || ts.isFunctionExpression(arg))
      const body = callback && ts.isBlock(callback.body) ? callback.body.statements : []
      const inner = collect(body, names, ranges)
      if (inner === 0) ranges.push([statement.getFullStart(), statement.getEnd()])
      else kept += inner
    }
  }
  return kept
}

function dropNested(ranges) {
  ranges.sort((a, b) => a[0] - b[0] || b[1] - a[1])
  const out = []
  for (const range of ranges) {
    if (out.length && range[0] >= out[out.length - 1][0] && range[1] <= out[out.length - 1][1]) continue
    out.push(range)
  }
  return out
}

function apply(text, edits) {
  let out = text
  for (const [start, end, replacement] of edits.sort((a, b) => b[0] - a[0])) out = out.slice(0, start) + replacement + out.slice(end)
  return out.replace(/\n{3,}/g, '\n\n')
}

function pruneImportNames(sf, names) {
  const edits = []
  for (const statement of sf.statements) {
    if (!ts.isImportDeclaration(statement) || !statement.importClause) continue
    const bindings = statement.importClause.namedBindings
    if (!bindings || !ts.isNamedImports(bindings)) continue
    const keep = bindings.elements.filter((element) => !names.has(element.name.text))
    if (keep.length === bindings.elements.length) continue
    if (!keep.length && !statement.importClause.name) edits.push([statement.getFullStart(), statement.getEnd(), ''])
    else edits.push([bindings.getStart(), bindings.getEnd(), `{ ${keep.map((element) => element.getText()).join(', ')} }`])
  }
  return edits
}

const deletedFiles = []
for (const [file, names] of missing) {
  const text = fs.readFileSync(file, 'utf8')
  const sf = parse(file, text)
  const ranges = []
  const kept = collect(sf.statements, names, ranges)
  if (kept === 0 && /\.test\./.test(file)) {
    fs.unlinkSync(file)
    deletedFiles.push(path.relative(root, file))
    continue
  }
  const edits = [...dropNested(ranges).map(([s, e]) => [s, e, '']), ...pruneImportNames(sf, names)]
  fs.writeFileSync(file, apply(text, edits), 'utf8')
  console.log(`${path.relative(root, file)}: 删用例 ${ranges.length} 条，剩 ${kept} 条`)
}
if (deletedFiles.length) console.log('整个删掉的测试文件：', deletedFiles.join(', '))

// 收尾：测试文件里因此没用的 import 和辅助函数
const edited = new Set([...missing.keys()].filter((file) => fs.existsSync(file)))
for (let round = 1; round <= 6; round += 1) {
  const config = ts.getParsedCommandLineOfConfigFile(path.join(root, 'tsconfig.app.json'), {}, { ...ts.sys, onUnRecoverableConfigFileDiagnostic: () => {} })
  const program = ts.createProgram({ rootNames: config.fileNames, options: { ...config.options, noEmit: true } })
  let changed = 0
  for (const file of edited) {
    const sourceFile = program.getSourceFile(file)
    if (!sourceFile) continue
    const diagnostics = ts.getPreEmitDiagnostics(program, sourceFile).filter((d) => [6133, 6192, 6196, 6198].includes(d.code))
    if (!diagnostics.length) continue
    const text = fs.readFileSync(file, 'utf8')
    const sf = parse(file, text)
    const edits = []
    const unusedNames = new Set()
    for (const diagnostic of diagnostics) {
      if (diagnostic.code === 6192) {
        const statement = sf.statements.find((s) => ts.isImportDeclaration(s) && s.getStart() <= diagnostic.start && diagnostic.start < s.getEnd())
        if (statement) edits.push([statement.getFullStart(), statement.getEnd(), ''])
      } else {
        unusedNames.add(text.slice(diagnostic.start, diagnostic.start + diagnostic.length))
      }
    }
    edits.push(...pruneImportNames(sf, unusedNames))
    for (const statement of sf.statements) {
      const declared = ts.isVariableStatement(statement)
        ? statement.declarationList.declarations.map((d) => (ts.isIdentifier(d.name) ? d.name.text : null))
        : ('name' in statement && statement.name && ts.isIdentifier(statement.name) ? [statement.name.text] : [])
      if (declared.length && declared.every((name) => name && unusedNames.has(name)) && !ts.isImportDeclaration(statement)) {
        edits.push([statement.getFullStart(), statement.getEnd(), ''])
      }
    }
    if (edits.length) {
      fs.writeFileSync(file, apply(text, dropNested(edits.map(([s, e]) => [s, e])).map(([s, e]) => [s, e, edits.find((x) => x[0] === s && x[1] === e)?.[2] ?? ''])), 'utf8')
      changed += edits.length
    }
  }
  console.log(`收尾第 ${round} 轮：${changed} 处`)
  if (!changed) break
}

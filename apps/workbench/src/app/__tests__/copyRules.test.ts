/**
 * 文案规范的兜底测试（规矩见 ../DESIGN.md 第三节）。
 * 扫的范围 = src/app 全部源码（9-29 回炉重造后唯一的界面）+ 它真正 import 到的 src/api、src/hooks、src/lib 文件（顺着 import 链算出来）。
 * 后者里的报错话、占位字也会出现在作者屏幕上，所以同一套规矩：
 *   检查展示残留，不对正常术语、语言和题材建立禁词表。
 */
import { describe, expect, it } from 'vitest'
import ts from 'typescript'

// 用打包器把 src 下所有源码当文本读进来，不依赖 Node 类型。旧壳目录和测试文件不算。
const SOURCES = import.meta.glob(
  [
    '../../**/*.ts',
    '../../**/*.tsx',
    '!../../**/*.test.*',
    '!../../**/__tests__/**',
    '!../../shell/**',
    '!../../studio/**',
    '!../../components/**',
    '!../../test/**',
    '!../../setupTests.ts',
    '!../../main.tsx',
    '!../../RootErrorBoundary.tsx',
  ],
  { query: '?raw', import: 'default', eager: true },
) as Record<string, string>

// ---- 把 glob 的相对键统一成 src/ 起头的路径，方便顺 import 链 ----
function normalize(parts: string[]): string {
  const out: string[] = []
  for (const part of parts) {
    if (part === '' || part === '.') continue
    if (part === '..') out.pop()
    else out.push(part)
  }
  return out.join('/')
}

/** glob 的键形如 '../../lib/words.ts'，相对本目录 src/app/__tests__。 */
function keyToSrcPath(key: string): string {
  return normalize(['src', 'app', '__tests__', ...key.split('/')])
}

const FILES = new Map<string, string>()
for (const [key, raw] of Object.entries(SOURCES)) FILES.set(keyToSrcPath(key), raw)

function resolveImport(fromFile: string, spec: string): string | null {
  if (!spec.startsWith('.')) return null
  const dir = fromFile.split('/').slice(0, -1)
  const base = normalize([...dir, ...spec.split('/')])
  for (const candidate of [base, `${base}.ts`, `${base}.tsx`, `${base}/index.ts`, `${base}/index.tsx`]) {
    if (FILES.has(candidate)) return candidate
  }
  return null
}

/** 从 src/app 出发，顺着 import 链把界面真正用到的底层文件都算进来。 */
function appClosure(): string[] {
  const queue = [...FILES.keys()].filter((path) => path.startsWith('src/app/'))
  const seen = new Set(queue)
  while (queue.length > 0) {
    const file = queue.pop() as string
    const raw = FILES.get(file) ?? ''
    for (const match of raw.matchAll(/from\s+['"]([^'"]+)['"]|import\(\s*['"]([^'"]+)['"]\s*\)/g)) {
      const target = resolveImport(file, match[1] ?? match[2] ?? '')
      if (target && !seen.has(target)) {
        seen.add(target)
        queue.push(target)
      }
    }
  }
  return [...seen].sort()
}

interface Found {
  file: string
  line: number
  text: string
}

function visibleStrings(file: string, raw: string): Found[] {
  const source = ts.createSourceFile(file, raw, ts.ScriptTarget.Latest, true,
    file.endsWith('.tsx') ? ts.ScriptKind.TSX : ts.ScriptKind.TS)
  const found: Found[] = []
  const visit = (node: ts.Node) => {
    // Read literal display text, not expressions such as stringify(value, null)
    // or JSX's : null branch between two quoted attributes.
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)
      || ts.isTemplateHead(node) || ts.isTemplateMiddle(node) || ts.isTemplateTail(node)
      || ts.isJsxText(node)) {
      const text = node.text.trim()
      const attribute = ts.isJsxAttribute(node.parent)
        && ['title', 'placeholder', 'aria-label', 'alt'].includes(node.parent.name.getText(source))
      if (/[一-鿿]/.test(text) || attribute) {
        found.push({ file, line: source.getLineAndCharacterOfPosition(node.getStart(source)).line + 1, text })
      }
    }
    ts.forEachChild(node, visit)
  }
  visit(source)
  return found
}

describe('界面文案规范', () => {
  it('区分真实文案和模板表达式、JSX 空分支里的代码', () => {
    const raw = 'const text = `结构化结果：${JSON.stringify(value, null, 2)}`; const view = <div>{dirty ? <span className="muted">未保存</span> : null}<span className="spacer" /></div>'
    const text = visibleStrings('sample.tsx', raw).map((item) => item.text)
    expect(text).toContain('结构化结果：')
    expect(text).toContain('未保存')
    expect(text.join('')).not.toContain('null')
    expect(visibleStrings('sample.tsx', '<div title="undefined">结果：null</div>').map((item) => item.text))
      .toEqual(['undefined', '结果：null'])
  })
  const closure = appClosure()
  // src/api 里的是接口契约校验的报错（「Candidate Output schema 不匹配」这类），它们描述的是程序间的约定，
  // 不是给作者看的话；到作者面前的那一层必须过 authorFacingError（authorFacingBoundary.test.ts 兜住）。所以这里只扫 app + hooks + lib。
  const files = closure
    .filter((path) => !path.startsWith('src/api/'))
    .map((path) => [path, FILES.get(path) ?? ''] as const)

  it('扫到了界面的文件和它真正用到的底层文件', () => {
    expect(closure.some((path) => path === 'src/app/strings.ts')).toBe(true)
    expect(closure.some((path) => path === 'src/app/text.ts')).toBe(true)
    expect(closure.some((path) => path.startsWith('src/hooks/'))).toBe(true)
    expect(closure.some((path) => path.startsWith('src/lib/'))).toBe(true)
    expect(closure.some((path) => path.startsWith('src/shell/') || path.startsWith('src/studio/') || path.startsWith('src/components/'))).toBe(false)
  })

  it('作者能看见的字里没有未解析的模板泄露（如 undefined、null、NaN、[object Object]）', () => {
    const templateLeaks = ['undefined', 'null', 'NaN', '[object Object]']
    const violations: string[] = []
    for (const [file, raw] of files) {
      for (const item of visibleStrings(file, raw)) {
        for (const leak of templateLeaks) {
          if (item.text.includes(leak)) {
            violations.push(`${item.file}:${item.line} 「${item.text}」 包含模板残留 ${leak}`)
          }
        }
      }
    }
    expect(violations, violations.join('\n')).toEqual([])
  })
})

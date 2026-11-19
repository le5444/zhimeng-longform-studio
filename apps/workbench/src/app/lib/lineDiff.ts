/**
 * 把「修改前 / 修改后」两段文字对齐成逐行的对照：没动的行不上色，
 * 删掉的行浅红底、新加的行浅绿底，作者一眼看出哪几行变了。
 *
 * 算法是经典的最长公共子序列（LCS），按行对齐；两段总共几千行以内都够用
 * （一处改动通常就是几段话）。超出预算就退化成「整段红 / 整段绿」，不卡界面。
 */

export type DiffRowKind = 'same' | 'del' | 'add'

export interface DiffRow {
  kind: DiffRowKind
  text: string
}

// Limit the alignment table to about 4 MiB; wrapping belongs to CSS and must
// never remove indentation or create source lines that did not exist.
const MAX_CELLS = 1_000_000

export function diffLines(before: string, after: string): DiffRow[] {
  const a = before === '' ? [] : before.split('\n')
  const b = after === '' ? [] : after.split('\n')
  let start = 0
  while (start < a.length && start < b.length && a[start] === b[start]) start++
  let endA = a.length
  let endB = b.length
  while (endA > start && endB > start && a[endA - 1] === b[endB - 1]) { endA--; endB-- }
  return [
    ...a.slice(0, start).map((text): DiffRow => ({ kind: 'same', text })),
    ...alignLines(a.slice(start, endA), b.slice(start, endB)),
    ...a.slice(endA).map((text): DiffRow => ({ kind: 'same', text })),
  ]
}

function alignLines(a: string[], b: string[]): DiffRow[] {
  if (a.length === 0 && b.length === 0) return []
  if (a.length === 0) return b.map((text) => ({ kind: 'add', text }))
  if (b.length === 0) return a.map((text) => ({ kind: 'del', text }))
  // 预算外的巨块：不做对齐，整段倒出（仍分红绿两色）。
  if ((a.length + 1) * (b.length + 1) > MAX_CELLS) {
    return [...a.map((text): DiffRow => ({ kind: 'del', text })), ...b.map((text): DiffRow => ({ kind: 'add', text }))]
  }

  // dp[i][j] = a 的前 i 行与 b 的前 j 行的公共行数。
  const cols = b.length + 1
  const dp = new Uint32Array((a.length + 1) * cols)
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      dp[i * cols + j] = a[i] === b[j]
        ? dp[(i + 1) * cols + j + 1] + 1
        : Math.max(dp[(i + 1) * cols + j], dp[i * cols + j + 1])
    }
  }

  const rows: DiffRow[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ kind: 'same', text: a[i] })
      i += 1
      j += 1
    } else if (dp[(i + 1) * cols + j] >= dp[i * cols + j + 1]) {
      rows.push({ kind: 'del', text: a[i] })
      i += 1
    } else {
      rows.push({ kind: 'add', text: b[j] })
      j += 1
    }
  }
  while (i < a.length) { rows.push({ kind: 'del', text: a[i] }); i += 1 }
  while (j < b.length) { rows.push({ kind: 'add', text: b[j] }); j += 1 }
  return rows
}

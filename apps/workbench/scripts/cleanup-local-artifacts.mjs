import { lstat, mkdir, readdir, realpath, rm, writeFile } from 'node:fs/promises'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const appRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const repoRoot = path.resolve(appRoot, '../..')
const runtimeRoot = path.join(repoRoot, '.runtime')
// walk-root-20260929.mjs still uses this directory as its default fixture.
const protectedNames = new Set(['e2e-walk-20260926'])
const args = process.argv.slice(2)
if (args.some((arg) => !['--apply', '--dry-run'].includes(arg))) {
  throw new Error('Usage: cleanup-local-artifacts.mjs [--dry-run | --apply]')
}
if (args.includes('--apply') && args.includes('--dry-run')) {
  throw new Error('--apply and --dry-run cannot be combined')
}
const apply = args.includes('--apply')
const archiveRoot = path.join(
  repoRoot,
  '历史归档',
  '旧验证副本',
  'services-writing-runtime-runtime',
  'kernel-harness',
)
const manifestPath = path.join(
  repoRoot,
  '.backup',
  `cleanup-local-artifacts-${new Date().toISOString().replace(/[:.]/g, '-')}.json`,
)

function inside(child, parent) {
  const relative = path.relative(parent, child)
  return relative !== '' && !relative.startsWith('..') && !path.isAbsolute(relative)
}

async function e2eEntries() {
  try {
    const entries = await readdir(runtimeRoot, { withFileTypes: true })
    return entries
      .filter((entry) => entry.name.startsWith('e2e-') && !protectedNames.has(entry.name))
      .map((entry) => path.join(runtimeRoot, entry.name))
  } catch (error) {
    if (error?.code === 'ENOENT') return []
    throw error
  }
}

async function bytes(paths) {
  let total = 0
  for (const target of paths) {
    const info = await lstat(target)
    if (info.isSymbolicLink()) throw new Error(`Refusing linked cleanup content: ${target}`)
    if (info.isFile()) total += info.size
    else {
      const children = await readdir(target, { withFileTypes: true })
      total += await bytes(children.map((child) => path.join(target, child.name)))
    }
  }
  return total
}

const targets = [...(await e2eEntries()), archiveRoot]
for (const target of targets) {
  if (!inside(target, repoRoot)) throw new Error(`清理目标越过项目边界: ${target}`)
}

const existing = []
for (const target of targets) {
  try {
    const info = await lstat(target)
    const resolved = await realpath(target)
    const relative = path.relative(repoRoot, resolved)
    if (info.isSymbolicLink() || !inside(resolved, repoRoot) || relative !== path.relative(repoRoot, target)) {
      throw new Error(`Refusing linked cleanup target: ${target}`)
    }
    if (!info.isDirectory()) throw new Error(`Cleanup target is not a directory: ${target}`)
    existing.push(target)
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
}

const record = {
  created_at: new Date().toISOString(),
  targets: existing,
  target_count: existing.length,
  bytes_before: await bytes(existing),
  protected_targets: [...protectedNames].map((name) => path.join(runtimeRoot, name)),
  active_runtime_db: path.join(runtimeRoot, 'workbench.sqlite3'),
  note: '候选目录须结合活动任务和内容核对。默认只预览；--apply 删除列出的候选，保留已知仍被引用的走查来源。不清理系统 Temp。',
}

if (!apply) {
  console.log(JSON.stringify(record, null, 2))
  process.exit(0)
}

await mkdir(path.dirname(manifestPath), { recursive: true })
await writeFile(manifestPath, `${JSON.stringify(record, null, 2)}\n`, { encoding: 'utf8', flag: 'wx' })
await Promise.all(existing.map((target) => rm(target, { recursive: true, force: true })))
const remaining = []
for (const target of existing) {
  try {
    await lstat(target)
    remaining.push(target)
  } catch (error) {
    if (error?.code !== 'ENOENT') throw error
  }
}
if (remaining.length) throw new Error(`清理后仍有目标残留: ${remaining.join(', ')}`)
console.log(`已清理 ${existing.length} 个目标，删除前文件大小合计 ${(record.bytes_before / 1024 / 1024).toFixed(2)} MiB；未测量磁盘净释放量。`)
console.log(`清理记录: ${manifestPath}`)

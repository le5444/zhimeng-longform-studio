import assert from 'node:assert/strict'
import { execFileSync, spawnSync } from 'node:child_process'
import { copyFile, mkdir, mkdtemp, readFile, readdir, rm, symlink, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import test from 'node:test'

const source = fileURLToPath(new URL('./cleanup-local-artifacts.mjs', import.meta.url))

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), 'cleanup-local-artifacts-check-'))
  t.after(() => rm(root, { recursive: true, force: true }))
  const script = path.join(root, 'apps/workbench/scripts/cleanup-local-artifacts.mjs')
  await mkdir(path.dirname(script), { recursive: true })
  await copyFile(source, script)
  for (const name of ['e2e-old', 'e2e-walk-20260926', 'author-projects']) {
    await mkdir(path.join(root, '.runtime', name), { recursive: true })
    await writeFile(path.join(root, '.runtime', name, 'keep.txt'), name)
  }
  await writeFile(path.join(root, '.runtime/workbench.sqlite3'), 'formal database sentinel')
  return { root, script, run: (...args) => execFileSync(process.execPath, [script, ...args], { encoding: 'utf8' }) }
}

for (const args of [[], ['--dry-run']]) {
  test(`preview ${args.join(' ') || 'by default'} leaves files and manifests unchanged`, async (t) => {
    const { root, run } = await fixture(t)
    const plan = JSON.parse(run(...args))
    assert.deepEqual(plan.targets, [path.join(root, '.runtime/e2e-old')])
    assert.deepEqual(plan.protected_targets, [path.join(root, '.runtime/e2e-walk-20260926')])
    assert.equal(await readFile(path.join(root, '.runtime/e2e-old/keep.txt'), 'utf8'), 'e2e-old')
    assert.equal((await readdir(root)).includes('.backup'), false)
  })
}

test('explicit apply preserves referenced fixtures and formal state', async (t) => {
  const { root, run } = await fixture(t)
  run('--apply')
  assert.equal((await readdir(path.join(root, '.runtime'))).includes('e2e-old'), false)
  assert.equal(await readFile(path.join(root, '.runtime/e2e-walk-20260926/keep.txt'), 'utf8'), 'e2e-walk-20260926')
  assert.equal(await readFile(path.join(root, '.runtime/author-projects/keep.txt'), 'utf8'), 'author-projects')
  assert.equal(await readFile(path.join(root, '.runtime/workbench.sqlite3'), 'utf8'), 'formal database sentinel')
  const manifests = await readdir(path.join(root, '.backup'))
  assert.equal(manifests.length, 1)
  const manifest = JSON.parse(await readFile(path.join(root, '.backup', manifests[0]), 'utf8'))
  assert.deepEqual(manifest.targets, [path.join(root, '.runtime/e2e-old')])
})

for (const nested of [false, true]) {
  test(`linked ${nested ? 'content' : 'target'} stops before any deletion or manifest write`, async (t) => {
    const { root, script } = await fixture(t)
    const target = path.join(root, '.runtime/author-projects')
    const link = path.join(root, '.runtime', nested ? 'e2e-old/linked' : 'e2e-linked')
    await symlink(target, link, process.platform === 'win32' ? 'junction' : 'dir')
    const result = spawnSync(process.execPath, [script, '--apply'], { encoding: 'utf8' })
    assert.notEqual(result.status, 0)
    assert.match(result.stderr, /Refusing linked cleanup/)
    assert.equal(await readFile(path.join(target, 'keep.txt'), 'utf8'), 'author-projects')
    assert.equal(await readFile(path.join(root, '.runtime/e2e-old/keep.txt'), 'utf8'), 'e2e-old')
    assert.equal((await readdir(root)).includes('.backup'), false)
  })
}

test('conflicting options cannot turn a preview into a deletion', async (t) => {
  const { root, script } = await fixture(t)
  const result = spawnSync(process.execPath, [script, '--apply', '--dry-run'], { encoding: 'utf8' })
  assert.notEqual(result.status, 0)
  assert.match(result.stderr, /cannot be combined/)
  assert.equal(await readFile(path.join(root, '.runtime/e2e-old/keep.txt'), 'utf8'), 'e2e-old')
})

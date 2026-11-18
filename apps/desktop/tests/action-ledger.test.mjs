import { test } from 'node:test'
import assert from 'node:assert/strict'
import { mkdtemp, readFile, rm, writeFile } from 'node:fs/promises'
import os from 'node:os'
import path from 'node:path'
import { ActionLedger } from '../src/action-ledger.mjs'

test('lost reply and restart retrieve a receipt without repeating a side effect', async () => {
  const directory = await mkdtemp(path.join(os.tmpdir(), 'zhimeng-browser-receipt-'))
  try {
    const ledger = new ActionLedger(directory); await ledger.restore()
    const target = path.join(directory, 'effect.txt'); let calls = 0
    const effect = async () => { calls++; await writeFile(target, String(calls)); return { saved: true } }
    const request = { action: 'click', ref: '1' }
    const [first, duplicate] = await Promise.all([ledger.execute('s1', 'request-123', request, effect), ledger.execute('s1', 'request-123', request, effect)])
    assert.equal(first.status, 'completed'); assert.deepEqual(first, duplicate)
    const reopened = new ActionLedger(directory); await reopened.restore()
    assert.deepEqual(await reopened.execute('s1', 'request-123', request, effect), first)
    assert.equal(await readFile(target, 'utf8'), '1'); assert.equal(calls, 1)
    assert.equal(reopened.get('other', 'request-123'), null)
    assert.throws(() => reopened.execute('s1', 'request-123', { action: 'reload' }, effect), /不同的请求/)
    reopened.records['request-crash'] = { request_id: 'request-crash', session_id: 's1', fingerprint: first.fingerprint, status: 'started' }; await reopened.save()
    const crashed = new ActionLedger(directory); await crashed.restore()
    assert.equal((await crashed.execute('s1', 'request-crash', request, effect)).status, 'started')
    assert.equal(calls, 1)
  } finally { await rm(directory, { recursive: true, force: true }) }
})

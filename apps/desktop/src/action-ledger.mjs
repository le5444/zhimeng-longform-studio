import { createHash } from 'node:crypto'
import { mkdir, open, readFile, rename } from 'node:fs/promises'
import path from 'node:path'
import { BrowserConflict } from './browser-state.mjs'

export async function atomicJson(file, value) {
  const handle = await open(file + '.tmp', 'w', 0o600)
  try { await handle.writeFile(JSON.stringify(value, null, 2)); await handle.sync() }
  finally { await handle.close() }
  await rename(file + '.tmp', file)
}

// A started receipt can be uncertain after a crash. Reusing its identity never replays the action.
export class ActionLedger {
  constructor(directory) { this.file = path.join(directory, 'browser-actions.json'); this.records = {}; this.pending = new Map(); this.saving = Promise.resolve() }
  async restore() {
    await mkdir(path.dirname(this.file), { recursive: true })
    try { this.records = JSON.parse(await readFile(this.file, 'utf8')) }
    catch (error) { if (error.code !== 'ENOENT') throw error }
  }
  save() {
    const snapshot = structuredClone(this.records)
    const write = this.saving.then(() => atomicJson(this.file, snapshot))
    this.saving = write.catch(() => {})
    return write
  }
  get(sessionId, id) { const receipt = this.records[id]; return receipt?.session_id === sessionId ? receipt : null }
  execute(sessionId, id, request, operation) {
    if (typeof id !== 'string' || !/^[\w-]{8,160}$/.test(id)) throw new TypeError('浏览器操作需要持久请求编号。')
    const fingerprint = createHash('sha256').update(JSON.stringify(request)).digest('hex')
    const previous = this.records[id]
    if (previous) {
      if (previous.session_id !== sessionId || previous.fingerprint !== fingerprint) throw new BrowserConflict('request_conflict', '这个操作编号已经用于不同的请求。')
      return this.pending.get(id) || Promise.resolve(previous)
    }
    const receipt = { request_id: id, session_id: sessionId, fingerprint, status: 'started', started_at: new Date().toISOString() }
    this.records[id] = receipt
    const task = (async () => {
      try { await this.save() } catch (error) { delete this.records[id]; throw error }
      try { receipt.result = await operation(); receipt.status = 'completed' }
      catch (error) { receipt.status = 'failed'; receipt.error = { code: error.code || 'browser_error', message: error.message, status: error.status || 400 } }
      receipt.finished_at = new Date().toISOString()
      await this.save()
      return receipt
    })().finally(() => this.pending.delete(id))
    this.pending.set(id, task)
    return task
  }
}

import { useEffect, useRef, useState } from 'react'
import { inspectCommandRecovery, restoreCommandFile, type CommandRecovery as Recovery, type CommandRecoveryEntry, type CommandRecoveryRequest, type CommandRecoveryReceipt } from '../../api/commandSessions'
import { Btn } from '../ui'
import { DiffView } from '../components/DiffView'

const statusText = { available: '可恢复', unchanged: '与执行前一致', conflict: '后来有变化', unavailable: '无法核对' }

export default function CommandRecovery({ sessionId, commandId, active = true, onChanged }: {
  sessionId: string; commandId: string; active?: boolean; onChanged?: () => void | Promise<void>
}) {
  const [data, setData] = useState<Recovery | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const pending = useRef<CommandRecoveryRequest | null>(null)
  const alive = useRef(true)
  const changed = useRef(onChanged)
  changed.current = onChanged
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])

  const checked = async (path?: string) => {
    const next = await inspectCommandRecovery(sessionId, commandId, path)
    if (alive.current) setData(next)
    return next
  }
  const inspect = async (path?: string) => {
    setBusy(true); setError(null)
    try { await checked(path) }
    catch (reason) { if (alive.current) setError(reason instanceof Error ? reason.message : '核对未完成，请重试。') }
    finally { if (alive.current) setBusy(false) }
  }
  const acknowledge = async (receipt: CommandRecoveryReceipt) => {
    if (!alive.current) return
    setError(null)
    setNotice(receipt.status === 'applied' ? '恢复已保存，可在「修改」中查看并撤回本次恢复。'
      : receipt.status === 'undone' ? '这次恢复已被撤回，没有重复写入。' : '恢复结果尚未确认，备份和记录已保留，请在「修改」中核对。')
    if (receipt.status !== 'unconfirmed') pending.current = null
    try { await changed.current?.() }
    catch { if (alive.current) setNotice('恢复记录已保存，但文件列表刷新失败。请重新打开文件核对。') }
  }
  const restore = async (entry: CommandRecoveryEntry) => {
    const request = pending.current?.path === entry.path && pending.current.revision === entry.revision
      ? pending.current : { path: entry.path, revision: entry.revision, request_id: crypto.randomUUID() }
    pending.current = request
    setBusy(true); setError(null); setNotice(null)
    try {
      const receipt = await restoreCommandFile(sessionId, commandId, request)
      await acknowledge(receipt)
      if (!alive.current) return
      try { await checked(entry.path) }
      catch { if (alive.current) setError('恢复记录已返回，当前文件重新核对失败，请重试核对。') }
    } catch (reason) {
      if (!alive.current) return
      setError(reason instanceof Error ? reason.message : '恢复未确认，请重新核对。')
      // Read back the original durable intent after a lost response; never resend automatically.
      try {
        const current = await checked(entry.path)
        const receipt = current.restorations.find(item => item.request_id === request.request_id)
        if (receipt) await acknowledge(receipt)
      } catch { /* Keep the original error and pending request identity. */ }
    } finally { if (alive.current) setBusy(false) }
  }
  const selected = data?.entries.find(entry => entry.path === data.previewed_path)
  return <section className="zm-command-recovery" aria-label="中断后的文件恢复">
    <Btn size="sm" busy={busy} disabled={!active} onClick={() => void inspect(data?.previewed_path ?? undefined)}>{data ? '重新核对文件与备份' : '核对文件与备份'}</Btn>
    {notice ? <p role="status">{notice}</p> : null}
    {error ? <p role="alert" className="zm-command-error">{error}</p> : null}
    {data ? <>
      <p>{data.source === 'current_check'
        ? '中断时没有完整的结束快照。以下比较执行前备份与当前文件，变化来源尚未确认。请逐项查看；恢复会替换当前显示的版本，并保留本次恢复的撤回记录。'
        : '以下核对命令结束时保存的快照。之后又有变化的文件会保留，不自动覆盖。'}</p>
      {data.entries.length ? <ul className="zm-command-recovery-list">{data.entries.map(entry => <li key={entry.path}>
        <button type="button" disabled={busy || !active} aria-pressed={selected?.path === entry.path} onClick={() => void inspect(entry.path)}>
          <span>{entry.path}</span><small>{statusText[entry.status]}</small>
        </button>
      </li>)}</ul> : <p>已核对范围内没有可恢复的普通文件。</p>}
      {selected ? <div className="zm-command-recovery-preview">
        <p><strong>{selected.path}</strong></p>
        <p>对照：当前版本 → 执行前版本</p>
        {selected.note ? <p>{selected.note}</p> : null}
        {selected.current_text !== null && selected.before_text !== null
          ? <DiffView before={selected.current_text} after={selected.before_text} />
          : <p>未提供完整文字预览。当前文件：{selected.current_sha256 ? `SHA-256 ${selected.current_sha256}` : '不存在'}；执行前：{selected.before_sha256 ? `SHA-256 ${selected.before_sha256}` : '不存在'}。</p>}
        {selected.status === 'available' ? <Btn size="sm" busy={busy} disabled={!active || Boolean(error)} onClick={() => void restore(selected)}>
          {selected.action === 'remove' ? '移除此文件，恢复执行前状态' : '恢复此文件到执行前版本'}
        </Btn> : null}
      </div> : null}
      {data.problems.map((problem, index) => <p key={index} className="zm-command-note">{problem}</p>)}
      {data.restorations.length ? <details><summary>本命令的恢复记录（{data.restorations.length}）</summary>
        {data.restorations.map(receipt => <p key={receipt.request_id}>{receipt.path} · {receipt.status === 'applied' ? '已恢复' : receipt.status === 'undone' ? '恢复已撤回' : '待核对'}，可在「修改」中查看。</p>)}
      </details> : null}
    </> : null}
  </section>
}

import type { CommandSessionSummary } from '../../api/commandSessions'
import CommandRecovery from './CommandRecovery'

export default function CommandFiles({ tracking, sessionId, commandId, active, onChanged }: {
  tracking: CommandSessionSummary['file_tracking']; sessionId?: string; commandId?: string; active?: boolean; onChanged?: () => void | Promise<void>
}) {
  if (!tracking) return null
  const label = tracking.status === 'observing' ? '正在观察所选文件的变化'
    : tracking.status === 'recorded' ? `已记录 ${tracking.file_count ?? 0} 项文件变化`
    : tracking.status === 'partial' ? `已记录 ${tracking.file_count ?? 0} 项变化，部分路径无法核对`
    : '文件变化尚未完整核对'
  return <details className="zm-command-metadata zm-command-files">
    <summary>{label}</summary>
    {tracking.change_id ? <p>可在右栏「修改」中查看和撤回这些文件变化。</p> : null}
    <p>{tracking.note ?? '只观察所选路径，范围外的命令影响未包含。'}</p>
    <ul>{tracking.paths.map(path => <li key={path}>{path}</li>)}</ul>
    {tracking.problems.map((problem, i) => <p key={i}>{problem.path}：{problem.reason}</p>)}
    {tracking.id && sessionId && commandId && ['interrupted', 'unverified', 'partial'].includes(tracking.status)
      ? <CommandRecovery key={`${sessionId}:${commandId}`} sessionId={sessionId} commandId={commandId} active={active} onChanged={onChanged} /> : null}
  </details>
}

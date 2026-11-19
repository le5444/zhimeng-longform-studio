import { Brain, ChevronRight } from 'lucide-react'
import type { MemoryChangeRecord } from '../../api/memoryChanges'
import { SourceLink } from './ConversationSources'

export function MemoryChanges({ sessionId, changes, onManage }: { sessionId: string; changes: MemoryChangeRecord[]; onManage: () => void }) {
  if (!changes.length) return null
  return <details className="zm-memory-changes">
    <summary><ChevronRight className="zm-memory-disclosure" aria-hidden /><Brain aria-hidden />记忆变更 · {changes.length} 项</summary>
    <ul>{changes.map((change, index) => <li key={`${change.created_at}:${index}`}>
      <div className="zm-memory-change-heading"><strong>{{ add: '新增', replace: '更新', remove: '移除' }[change.action]}</strong><time dateTime={change.created_at}>{new Date(change.created_at).toLocaleString()}</time></div>
      {change.old_text ? <div className="zm-memory-change-old"><span>原来</span><p>{change.old_text}</p></div> : null}
      {change.text ? <div><span>{change.action === 'replace' ? '现在' : '内容'}</span><p>{change.text}</p></div> : null}
      {change.evidence ? <blockquote><span>本轮原话</span><p>{change.evidence}</p></blockquote> : null}
      {change.source_message_id ? <SourceLink target={{ sessionId, messageId: change.source_message_id }} /> : null}
    </li>)}</ul>
    <button type="button" className="zm-link" onClick={onManage}>管理记忆</button>
  </details>
}

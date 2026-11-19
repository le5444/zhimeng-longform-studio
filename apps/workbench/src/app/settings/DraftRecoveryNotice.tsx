import type { useDraftRecovery } from './draftRecovery'

export function DraftRecoveryNotice({ recovery }: { recovery: Pick<ReturnType<typeof useDraftRecovery>, 'error' | 'unreadable' | 'retained' | 'recovered' | 'retry' | 'removeUnreadable'> }) {
  if (recovery.error) return <div className="zm-setting-feedback is-error" role="alert">
    <span>{recovery.error}</span>{' '}
    <button type="button" className="zm-link" onClick={recovery.unreadable ? recovery.removeUnreadable : recovery.retry}>
      {recovery.unreadable ? '移除旧备份，保留当前输入' : '重试备份'}
    </button>
  </div>
  if (!recovery.retained) return null
  return <p className="zm-faint" role="status">{recovery.recovered ? '已恢复未保存的草稿。' : ''}未保存的输入（含新填密钥）已暂存于此标签页，刷新可恢复；点击保存后才会生效。</p>
}

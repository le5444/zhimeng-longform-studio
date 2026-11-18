import { Button } from './kit'

export interface SettingsDifference { label: string; saved: string; draft: string }

/** Explicit comparison, without displaying secret values or saving on resolve. */
export function SettingsConflict({ rows, deleted, busy, onReload, onKeep, onCopy }: {
  rows: SettingsDifference[]
  deleted?: boolean
  busy?: boolean
  onReload: () => void
  onKeep: () => void
  onCopy?: () => void
}) {
  return <section className="zm-settings-conflict" aria-label="配置变更比较" role="status">
    <strong>{deleted ? '这个连接已在其他地方移除' : '已保存的配置发生了变化'}</strong>
    <p>你的输入仍保留。{deleted ? (onCopy ? '可以把草稿另存为新连接。' : '关闭前可以复制需要保留的内容。') : '核对下面的差异后，选择继续编辑的内容；此处不会自动保存。'}</p>
    {!deleted ? <details><summary>比较最新配置与我的草稿</summary>
      {rows.length ? <div className="zm-settings-comparison"><table><thead><tr><th scope="col">项目</th><th scope="col">最新配置</th><th scope="col">我的草稿</th></tr></thead>
        <tbody>{rows.map(row => <tr key={row.label}><th scope="row">{row.label}</th><td>{row.saved || '未填写'}</td><td>{row.draft || '未填写'}</td></tr>)}</tbody></table></div>
        : <p>连接凭据、模型例外或其他保存字段有变化。密钥不会在这里显示。</p>}
    </details> : null}
    <div className="zm-btn-row">
      {deleted && onCopy ? <Button small disabled={busy} onClick={onCopy}>另存为新连接</Button> : null}
      {!deleted ? <Button small disabled={busy} onClick={onKeep}>合并我的修改，继续编辑</Button> : null}
      <Button small variant="quiet" disabled={busy} onClick={onReload}>{deleted ? '放弃此草稿' : '放弃草稿，使用最新配置'}</Button>
    </div>
  </section>
}

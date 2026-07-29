import { useEffect } from 'react'
import { AlertTriangle, LoaderCircle, Network, RefreshCw } from 'lucide-react'
import { useProjectAssets } from '../hooks/useProjectAssets'

function kindLabel(kind: string): string {
  if (kind === 'character') return '人物'
  if (kind === 'plot') return '剧情'
  if (kind === 'foreshadowing') return '伏笔'
  if (kind === 'timeline') return '时间线'
  return kind
}

export function ProjectAssetsPanel({
  enabled = true,
  projectId,
}: {
  enabled?: boolean
  projectId: string | null
}) {
  const assets = useProjectAssets(projectId, enabled && Boolean(projectId))

  useEffect(() => {
    if (!enabled || !projectId) return
    void assets.refresh()
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [enabled, projectId])

  if (!enabled) {
    return (
      <div
        className="inspector-body project-assets-panel"
        role="tabpanel"
        id="inspector-panel-assets"
        aria-labelledby="inspector-tab-assets"
      >
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>Writing Runtime 未就绪</span>
        </div>
      </div>
    )
  }

  if (!projectId) {
    return (
      <div
        className="inspector-body project-assets-panel"
        role="tabpanel"
        id="inspector-panel-assets"
        aria-labelledby="inspector-tab-assets"
      >
        <div className="distillation-empty">
          <AlertTriangle size={16} />
          <span>请先绑定小说项目</span>
        </div>
      </div>
    )
  }

  const index = assets.payload?.index
  return (
    <div
      className="inspector-body project-assets-panel"
      role="tabpanel"
      id="inspector-panel-assets"
      aria-labelledby="inspector-tab-assets"
      aria-busy={assets.busy}
    >
      <header className="project-assets-toolbar">
        <div>
          <strong>领域资产索引</strong>
          <span>
            {index
              ? `${index.asset_count} 项 · 关系 ${index.relationship_count ?? 0} · 问题 ${index.issue_count ?? 0} · 关系图未完成`
              : assets.busy
                ? '加载中'
                : '尚未重建'}
          </span>
        </div>
        <button
          type="button"
          className="quiet-button"
          disabled={assets.busy}
          onClick={() => void assets.rebuild()}
        >
          {assets.busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
          <span>重建</span>
        </button>
      </header>

      {assets.error && (
        <div className="source-search-notice is-error" role="alert">
          <AlertTriangle size={15} />
          <span>{assets.error}</span>
        </div>
      )}

      {index && index.assets.length === 0 && (
        <div className="distillation-empty">
          <Network size={16} />
          <span>未发现领域资产 markdown</span>
        </div>
      )}

      {index && index.assets.length > 0 && (
        <ul className="project-asset-list" aria-label="领域资产列表">
          {index.assets.map((item) => (
            <li key={item.asset_id}>
              <span className="project-asset-icon" aria-hidden="true">
                <Network size={14} />
              </span>
              <div>
                <strong>{item.title}</strong>
                <span>
                  {kindLabel(String(item.kind))} · {item.relative_path}
                </span>
              </div>
            </li>
          ))}
        </ul>
      )}

      {assets.payload && (
        <p className="project-assets-disclaimer">
          不宣称：{(assets.payload.not_claiming || []).slice(0, 4).join(' / ')}
        </p>
      )}
    </div>
  )
}

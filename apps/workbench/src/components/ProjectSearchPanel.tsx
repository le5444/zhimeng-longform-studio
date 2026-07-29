import {
  AlertTriangle,
  BookOpenText,
  Database,
  FileSearch,
  LoaderCircle,
  RefreshCw,
  Search,
} from 'lucide-react'
import type { ProjectSearchAuthority, ProjectSearchSourceType } from '../api/projectSearch'
import { useProjectSearch } from '../hooks/useProjectSearch'

const sourceTypeOptions: Array<{ value: ProjectSearchSourceType | 'all'; label: string }> = [
  { value: 'all', label: '全部来源' },
  { value: 'project_file', label: '项目真值' },
  { value: 'formal_knowledge', label: '正式知识' },
]

const authorityOptions: Array<{ value: ProjectSearchAuthority | 'all'; label: string }> = [
  { value: 'all', label: '全部权威' },
  { value: 'project_truth', label: '项目真值' },
  { value: 'formal_knowledge', label: '正式知识' },
  { value: 'reference', label: '参考资料' },
]

function authorityLabel(value: string): string {
  if (value === 'project_truth') return '项目真值'
  if (value === 'formal_knowledge') return '正式知识'
  if (value === 'reference') return '参考'
  return value
}

function sourceTypeLabel(value: string): string {
  if (value === 'project_file') return '项目文件'
  if (value === 'formal_knowledge') return '正式知识卡'
  return value
}

function compactHash(value: string): string {
  return value.slice(0, 8)
}

export function ProjectSearchPanel({
  enabled = true,
  projectId,
  onOpenProjectFile,
}: {
  enabled?: boolean
  projectId: string | null
  onOpenProjectFile?: (sourceId: string, relativePath: string | null) => void
}) {
  const search = useProjectSearch(projectId, enabled && Boolean(projectId))

  if (!enabled) {
    return (
      <div className="inspector-body project-search-panel" role="tabpanel" id="inspector-panel-search" aria-labelledby="inspector-tab-search">
        <div className="distillation-empty"><AlertTriangle size={16} /><span>Writing Runtime 未就绪</span></div>
      </div>
    )
  }

  if (!projectId) {
    return (
      <div className="inspector-body project-search-panel" role="tabpanel" id="inspector-panel-search" aria-labelledby="inspector-tab-search">
        <div className="distillation-empty"><AlertTriangle size={16} /><span>请先绑定小说项目</span></div>
      </div>
    )
  }

  return (
    <div
      className="inspector-body project-search-panel"
      role="tabpanel"
      id="inspector-panel-search"
      aria-labelledby="inspector-tab-search"
      aria-busy={search.busy}
    >
      <header className="distillation-healthbar">
        <span className="distillation-health-icon health-healthy">
          {search.busy ? <LoaderCircle size={15} className="spin" /> : <FileSearch size={15} />}
        </span>
        <div>
          <strong>项目检索</strong>
          <span>
            {search.manifest
              ? `索引 ${search.manifest.document_count} 条 · rev ${search.manifest.project_revision}`
              : search.result
                ? `命中 ${search.result.hit_count} · ${search.result.rebuilt_before_search ? '已自动重建' : '使用现有索引'}`
                : '跨项目真值与正式知识卡检索'}
          </span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="重建项目检索索引"
          title="重建索引"
          disabled={search.busy}
          onClick={() => void search.rebuild(true)}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      <form
        className="coverage-form"
        onSubmit={(event) => {
          event.preventDefault()
          void search.runSearch()
        }}
      >
        <label htmlFor="project-search-query">检索词</label>
        <textarea
          id="project-search-query"
          maxLength={512}
          placeholder="例如：阶段兑现 / 青崖 / 主角档案"
          value={search.query}
          disabled={search.busy}
          onChange={(event) => search.setQuery(event.target.value)}
        />
        <div className="coverage-form-actions">
          <select
            aria-label="来源类型"
            value={search.sourceType}
            disabled={search.busy}
            onChange={(event) => search.setSourceType(event.target.value as ProjectSearchSourceType | 'all')}
          >
            {sourceTypeOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
          <select
            aria-label="权威等级"
            value={search.authority}
            disabled={search.busy}
            onChange={(event) => search.setAuthority(event.target.value as ProjectSearchAuthority | 'all')}
          >
            {authorityOptions.map((option) => (
              <option key={option.value} value={option.value}>{option.label}</option>
            ))}
          </select>
        </div>
        <div className="coverage-form-actions">
          <button type="submit" disabled={search.busy || !search.query.trim()}>
            {search.searchState === 'loading' ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
            <span>{search.searchState === 'loading' ? '检索中' : '检索'}</span>
          </button>
          <button
            type="button"
            disabled={search.busy}
            onClick={() => void search.rebuild(true)}
          >
            {search.rebuildState === 'loading' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
            <span>{search.rebuildState === 'loading' ? '重建中' : '重建索引'}</span>
          </button>
        </div>
      </form>

      {(search.searchError || search.rebuildError) && (
        <div className="agent-inline-error" role="alert">
          <AlertTriangle size={14} />
          <span>{search.searchError ?? search.rebuildError}</span>
        </div>
      )}

      <div className="project-search-results" aria-label="检索结果">
        {search.searchState === 'idle' && !search.hits.length && (
          <div className="distillation-empty"><Search size={16} /><span>输入关键词后检索项目真值与正式知识</span></div>
        )}
        {search.searchState === 'ready' && search.hits.length === 0 && (
          <div className="distillation-empty"><Search size={16} /><span>没有匹配结果</span></div>
        )}
        {search.hits.map((hit) => {
          const openable = hit.source_type === 'project_file' && Boolean(onOpenProjectFile)
          return (
            <article className="project-search-hit" key={`${hit.source_type}:${hit.source_id}`}>
              <header>
                <span className="project-search-hit-icon">
                  {hit.source_type === 'formal_knowledge' ? <Database size={14} /> : <BookOpenText size={14} />}
                </span>
                <div>
                  <strong>{hit.title}</strong>
                  <span>
                    {sourceTypeLabel(hit.source_type)} · {authorityLabel(hit.authority)} · score {hit.score.toFixed(2)}
                  </span>
                </div>
              </header>
              <p>{hit.snippet}</p>
              <footer>
                <code title={hit.source_id}>{hit.source_id.slice(0, 18)}</code>
                <code title={hit.source_hash}>{compactHash(hit.source_hash)}</code>
                <span title={hit.reason}>{hit.reason}</span>
                {openable && (
                  <button
                    type="button"
                    className="quiet-button"
                    onClick={() => onOpenProjectFile?.(hit.source_id, hit.relative_path)}
                  >
                    打开
                  </button>
                )}
              </footer>
            </article>
          )
        })}
      </div>
    </div>
  )
}

import { useMemo, useRef, useState } from 'react'
import {
  BookOpen,
  FolderKanban,
  FolderPlus,
  LibraryBig,
  Loader2,
  RefreshCw,
  Search,
  TriangleAlert,
  X,
} from 'lucide-react'
import type { ProjectBinding } from '../api/projects'

interface BookshelfProps {
  bindings: ProjectBinding[]
  loading: boolean
  error: string | null
  onOpen: (projectId: string) => void
  onAdd: (file: File) => Promise<unknown>
  onRefresh: () => void
}

type ShelfView = 'books' | 'development'

const DEVELOPMENT_PROJECT_PATTERNS = [
  /^链路验证[_-]/i,
  /^压测[-_]/i,
  /^r\d+[-_]/i,
  /^phase\d+[-_]/i,
  /^四系统联动[-_]/i,
  /(?:验收|压力测试)/i,
]

function folderName(rootPath: unknown): string {
  if (typeof rootPath !== 'string' || rootPath.trim() === '') return '未命名作品'
  const normalized = rootPath.trim().replace(/[\\/]+$/, '')
  if (normalized === '') return '未命名作品'
  const parts = normalized.split(/[\\/]/)
  return parts.at(-1) || '未命名作品'
}

function rawDisplayName(binding: ProjectBinding): string {
  return binding.display_name?.trim() || folderName(binding.root_path)
}

function displayName(binding: ProjectBinding): string {
  return rawDisplayName(binding)
    .replace(/[\s_-]*(?:创作项目|创作草案)$/u, '')
    .trim() || '未命名作品'
}

function displayPath(rootPath: unknown): string {
  return typeof rootPath === 'string' && rootPath.trim() !== ''
    ? rootPath
    : '作品路径信息缺失，请重新加入书架'
}

function isDevelopmentProject(binding: ProjectBinding): boolean {
  const name = rawDisplayName(binding)
  return DEVELOPMENT_PROJECT_PATTERNS.some((pattern) => pattern.test(name))
}

function formatUpdatedAt(value: string): string {
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '更新时间未知'
  return `${parsed.getMonth() + 1}月${parsed.getDate()}日更新`
}

function coverTone(value: string): number {
  let hash = 0
  for (const character of value) {
    hash = ((hash << 5) - hash + character.codePointAt(0)!) | 0
  }
  return Math.abs(hash) % 6
}

export function Bookshelf({
  bindings,
  loading,
  error,
  onOpen,
  onAdd,
  onRefresh,
}: BookshelfProps) {
  const [adding, setAdding] = useState(false)
  const [addError, setAddError] = useState<string | null>(null)
  const [query, setQuery] = useState('')
  const [view, setView] = useState<ShelfView>('books')
  const fileInputRef = useRef<HTMLInputElement>(null)

  const { books, developmentProjects } = useMemo(() => {
    const nextBooks: ProjectBinding[] = []
    const nextDevelopmentProjects: ProjectBinding[] = []
    for (const binding of bindings) {
      if (isDevelopmentProject(binding)) {
        nextDevelopmentProjects.push(binding)
      } else {
        nextBooks.push(binding)
      }
    }
    return { books: nextBooks, developmentProjects: nextDevelopmentProjects }
  }, [bindings])

  const visibleBindings = view === 'books' ? books : developmentProjects
  const normalizedQuery = query.trim().toLocaleLowerCase('zh-CN')
  const showImportCard = view === 'books' && normalizedQuery === ''
  const filteredBindings = normalizedQuery === ''
    ? visibleBindings
    : visibleBindings.filter((binding) => (
        displayName(binding).toLocaleLowerCase('zh-CN').includes(normalizedQuery)
        || displayPath(binding.root_path).toLocaleLowerCase('zh-CN').includes(normalizedQuery)
      ))

  const submitAdd = async (file: File) => {
    if (adding) return
    setAdding(true)
    setAddError(null)
    try {
      await onAdd(file)
    } catch (caught) {
      setAddError(caught instanceof Error ? caught.message : '这份 TXT 没能导入')
    } finally {
      setAdding(false)
    }
  }

  return (
    <section className="bookshelf" aria-label="书架">
      <input
        ref={fileInputRef}
        className="bookshelf-file-input"
        type="file"
        accept=".txt,text/plain"
        aria-label="选择 TXT 作品"
        onChange={(event) => {
          const file = event.target.files?.[0]
          event.target.value = ''
          if (file !== undefined) void submitAdd(file)
        }}
      />
      <header className="bookshelf-header">
        <div className="bookshelf-heading">
          <div className="bookshelf-title-row">
            <span className="bookshelf-title-icon" aria-hidden="true">
              <LibraryBig size={20} />
            </span>
            <h1 className="bookshelf-title">我的书架</h1>
            <span className="bookshelf-count">{books.length} 本</span>
          </div>
          <p className="bookshelf-subtitle">你的长篇作品都在这里。</p>
        </div>

        <div className="bookshelf-tools">
          <label className="bookshelf-search">
            <Search size={17} aria-hidden="true" />
            <input
              type="search"
              value={query}
              placeholder={view === 'books' ? '搜索书名' : '搜索开发项目'}
              aria-label={view === 'books' ? '搜索书名' : '搜索开发项目'}
              onChange={(event) => setQuery(event.target.value)}
            />
            {query !== '' && (
              <button
                type="button"
                className="bookshelf-search-clear"
                aria-label="清空搜索"
                onClick={() => setQuery('')}
              >
                <X size={14} />
              </button>
            )}
          </label>
          <button
            type="button"
            className="bookshelf-tool-button"
            onClick={() => fileInputRef.current?.click()}
            disabled={adding}
            aria-label="导入 TXT 作品"
            title="导入 TXT 作品"
          >
            {adding ? <Loader2 size={17} className="spin" /> : <FolderPlus size={17} />}
          </button>
          <button
            type="button"
            className="bookshelf-tool-button"
            onClick={onRefresh}
            disabled={loading}
            aria-label="刷新书架"
            title="刷新书架"
          >
            <RefreshCw size={17} className={loading ? 'spin' : undefined} />
          </button>
        </div>
      </header>

      <div className="bookshelf-tabs" role="tablist" aria-label="书架分类">
        <button
          type="button"
          role="tab"
          aria-selected={view === 'books'}
          className={view === 'books' ? 'is-active' : ''}
          onClick={() => {
            setView('books')
            setQuery('')
          }}
        >
          作品 <span>{books.length}</span>
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={view === 'development'}
          className={view === 'development' ? 'is-active' : ''}
          onClick={() => {
            setView('development')
            setQuery('')
          }}
        >
          开发项目 <span>{developmentProjects.length}</span>
        </button>
      </div>

      {addError !== null && (
        <p className="bookshelf-error bookshelf-runtime-error" role="alert">
          <TriangleAlert size={13} /> {addError}
        </p>
      )}

      {error !== null && (
        <p className="bookshelf-error bookshelf-runtime-error" role="alert">
          <TriangleAlert size={13} /> {error}
        </p>
      )}

      {loading && bindings.length === 0 ? (
        <div className="bookshelf-loading" aria-label="正在读取书架">
          {Array.from({ length: 4 }, (_, index) => (
            <span key={index} />
          ))}
        </div>
      ) : filteredBindings.length === 0 && !showImportCard ? (
        <div className="bookshelf-empty">
          {query.trim() !== ''
            ? `没有找到“${query.trim()}”`
            : view === 'books'
              ? '书架里还没有作品'
              : '没有开发项目'}
        </div>
      ) : (
        <ul className={`bookshelf-grid ${view === 'development' ? 'is-development' : ''}`}>
          {showImportCard && (
            <li>
              <button
                type="button"
                className="book-import-card"
                onClick={() => fileInputRef.current?.click()}
                disabled={adding}
              >
                <span className="book-import-icon" aria-hidden="true">
                  {adding ? <Loader2 size={24} className="spin" /> : <FolderPlus size={24} />}
                </span>
                <span className="book-import-title">
                  {adding ? '正在导入' : '导入现有作品'}
                </span>
                <span className="book-import-copy">选择本机 TXT 文件</span>
              </button>
            </li>
          )}
          {filteredBindings.map((binding) => {
            const title = displayName(binding)
            const development = isDevelopmentProject(binding)
            return (
              <li key={binding.project_id}>
                <button
                  type="button"
                  className="book-card"
                  onClick={() => onOpen(binding.project_id)}
                >
                  <span
                    className={`book-cover book-cover-tone-${coverTone(title)} ${
                      development ? 'is-development' : ''
                    }`}
                  >
                    <span className="book-cover-icon" aria-hidden="true">
                      {development ? <FolderKanban size={28} /> : <BookOpen size={28} />}
                    </span>
                    <span className="book-cover-kicker">
                      {development ? '开发项目' : '织梦作品'}
                    </span>
                    <span className="book-cover-title">{title}</span>
                    <span className="book-cover-date">{formatUpdatedAt(binding.updated_at)}</span>
                  </span>
                  <span className="book-card-details">
                    <span className="book-card-title">{title}</span>
                    <span className="book-card-meta">
                      {binding.snapshot_ready ? `第 ${binding.revision} 版` : '待重新读取'}
                    </span>
                    {displayPath(binding.root_path) === '作品路径信息缺失，请重新加入书架' && (
                      <span className="book-card-path">
                        {displayPath(binding.root_path)}
                      </span>
                    )}
                  </span>
                </button>
              </li>
            )
          })}
        </ul>
      )}
    </section>
  )
}

import { useEffect, useMemo, useState } from 'react'
import {
  BookOpen,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  FileText,
  Search,
  X,
} from 'lucide-react'
import { volumes, type ChapterState } from '../data/workbenchFixture'
import type {
  ProjectChapter,
  ProjectInspection,
  ProjectVolume,
} from '../api/projects'

const stateIcon: Record<ChapterState, React.ReactNode> = {
  done: <CircleCheck size={13} />,
  draft: <CircleDashed size={13} />,
  review: <CircleAlert size={13} />,
  blocked: <CircleAlert size={13} />,
}

interface TreeChapter {
  id: string
  number: string
  title: string
  state: ChapterState
}

interface TreeVolume {
  id: string
  title: string
  progress: string
  chapters: TreeChapter[]
}

interface NavigatorProps {
  snapshot: ProjectInspection | null
  activeChapterId: string | null
  projectState: 'idle' | 'loading' | 'ready' | 'unbound' | 'error'
  demo: boolean
  onChapterSelect: (chapterId: string) => void
}

function chapterTitle(snapshot: ProjectInspection, chapter: ProjectChapter): string {
  const primaryId = chapter.prose_file_ids[0] ?? chapter.file_ids[0]
  const relativePath = snapshot.files.find((file) => file.file_id === primaryId)?.relative_path
  const fileName = relativePath?.split('/').at(-1)?.replace(/\.[^.]+$/, '')
  return fileName && !/^CH\d+$/i.test(fileName)
    ? fileName
    : `第 ${String(chapter.number).padStart(3, '0')} 章`
}

function volumeTitle(snapshot: ProjectInspection, volume: ProjectVolume): string {
  const file = snapshot.files.find((item) => item.volume_id === volume.volume_id)
  const folder = file?.relative_path.split('/')[1]
  return folder ?? `第 ${volume.number} 卷`
}

function projectTree(snapshot: ProjectInspection): TreeVolume[] {
  const chapterById = new Map(snapshot.chapters.map((chapter) => [chapter.chapter_id, chapter]))
  const toChapter = (chapter: ProjectChapter): TreeChapter => ({
    id: chapter.chapter_id,
    number: String(chapter.number).padStart(3, '0'),
    title: chapterTitle(snapshot, chapter),
    state: chapter.prose_file_ids.length > 0 ? 'draft' : 'blocked',
  })
  const result = snapshot.volumes.map((volume) => {
    const chapters = volume.chapter_ids
      .map((chapterId) => chapterById.get(chapterId))
      .filter((chapter): chapter is ProjectChapter => chapter !== undefined)
    return {
      id: volume.volume_id,
      title: volumeTitle(snapshot, volume),
      progress: `${chapters.filter((chapter) => chapter.prose_file_ids.length > 0).length} / ${chapters.length}`,
      chapters: chapters.map(toChapter),
    }
  })
  const unassigned = snapshot.chapters.filter((chapter) => chapter.volume_id === null)
  if (unassigned.length > 0) {
    result.push({
      id: 'unassigned',
      title: '未分卷章节',
      progress: `${unassigned.filter((chapter) => chapter.prose_file_ids.length > 0).length} / ${unassigned.length}`,
      chapters: unassigned.map(toChapter),
    })
  }
  return result
}

export function Navigator({
  snapshot,
  activeChapterId,
  projectState,
  demo,
  onChapterSelect,
}: NavigatorProps) {
  const [expanded, setExpanded] = useState(() => new Set(['v1', 'v2']))
  const [exampleActiveChapter, setExampleActiveChapter] = useState('c12')
  const [query, setQuery] = useState('')
  const fullTree = useMemo<TreeVolume[]>(
    () => (snapshot ? projectTree(snapshot) : demo ? volumes : []),
    [demo, snapshot],
  )

  // 974 章的书靠滚动是找不到章节的，搜索必须真的能用。
  // 章号和标题都能搜：输 "005" 或 "药膳" 都该命中。
  const tree = useMemo<TreeVolume[]>(() => {
    const keyword = query.trim().toLowerCase()
    if (!keyword) return fullTree
    return fullTree
      .map((volume) => ({
        ...volume,
        chapters: volume.chapters.filter(
          (chapter) =>
            chapter.title.toLowerCase().includes(keyword)
            || chapter.number.toLowerCase().includes(keyword),
        ),
      }))
      .filter((volume) => volume.chapters.length > 0)
  }, [fullTree, query])

  useEffect(() => {
    setExpanded(new Set(tree.map((volume) => volume.id)))
  }, [tree])

  const activeChapter = snapshot ? activeChapterId : demo ? exampleActiveChapter : null
  const selectChapter = snapshot ? onChapterSelect : demo ? setExampleActiveChapter : () => undefined
  const projectName = snapshot?.project.display_name ?? (projectState === 'error' ? '项目读取失败' : projectState === 'loading' ? '正在读取项目' : '雾港来信（示例）')
  const chapterCount = tree.reduce((total, volume) => total + volume.chapters.length, 0)

  const toggleVolume = (id: string) => {
    setExpanded((current) => {
      const next = new Set(current)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })
  }

  return (
    <aside className="navigator panel-mobile" data-mobile-pane="outline" aria-label="作品卷章树">
      <header className="panel-header">
        <div><span className="panel-kicker">作品</span><h2>{projectName}</h2></div>
      </header>
      <div className="tree-tools">
        <label className="search-field">
          <Search size={14} />
          <input
            aria-label="搜索章节"
            placeholder="搜索章号或标题"
            value={query}
            onChange={(event) => setQuery(event.target.value)}
          />
        </label>
        {query !== '' && (
          <button
            className="icon-button"
            type="button"
            title="清空搜索"
            aria-label="清空搜索"
            onClick={() => setQuery('')}
          >
            <X size={15} />
          </button>
        )}
      </div>
      <div className="tree-summary">
        <BookOpen size={14} />
        <span>
          {query
            ? `找到 ${chapterCount} 章`
            : `${tree.length} 卷 · ${chapterCount} 章`}
        </span>
        <strong>{snapshot ? `${snapshot.files.length} 文件` : '示例数据'}</strong>
      </div>
      {query !== '' && chapterCount === 0 && (
        <p className="tree-empty">没有匹配「{query}」的章节</p>
      )}
      <div className="volume-tree">
        {tree.map((volume) => {
          const isExpanded = expanded.has(volume.id)
          return (
            <section className="volume-group" key={volume.id}>
              <button className="volume-row" type="button" onClick={() => toggleVolume(volume.id)} aria-expanded={isExpanded}>
                {isExpanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                <span>{volume.title}</span><small>{volume.progress}</small>
              </button>
              {isExpanded && (
                <div className="chapter-list">
                  {volume.chapters.map((chapter) => (
                    <button
                      type="button"
                      className={`chapter-row state-${chapter.state}${activeChapter === chapter.id ? ' is-active' : ''}`}
                      aria-current={activeChapter === chapter.id ? 'page' : undefined}
                      key={chapter.id}
                      onClick={() => selectChapter(chapter.id)}
                    >
                      <FileText size={14} />
                      <span className="chapter-number">{chapter.number}</span>
                      <span className="chapter-title">{chapter.title}</span>
                      <span className="chapter-state" title={chapter.state}>{stateIcon[chapter.state]}</span>
                    </button>
                  ))}
                </div>
              )}
            </section>
          )
        })}
      </div>
      <footer className="navigator-footer"><span>{snapshot ? '只读项目快照' : projectState === 'loading' ? '正在读取项目' : projectState === 'error' ? '项目读取失败' : '尚未绑定 · 示例态'}</span><strong>{snapshot ? `rev ${snapshot.project.revision}` : 'demo'}</strong></footer>
    </aside>
  )
}

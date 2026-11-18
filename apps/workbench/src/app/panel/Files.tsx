import { useMemo, useRef, useState } from 'react'
import { ChevronDown, ChevronRight, FileText, Folder } from 'lucide-react'
import type { ProjectFile, ProjectInspection } from '../../api/projects'
import { A } from '../text'
import { Btn, type Notify } from '../ui'
import { resolveFileNavigation } from '../lib/fileNavigation'
import type { FileLocation } from '../lib/fileReferences'
import type { FileFocus } from '../lib/filePosition'
import type { FileQuotation } from '../lib/fileQuotation'
import { fileDraftKey, readFileDraft, sameFileDraft, writeFileDraft, type FileDraftState } from '../lib/fileDrafts'
import { FileView } from './FileView'

/**
 * 右栏「文件」：打开的文件夹里有什么，点开看，能直接改。
 *
 * 普通 agent 的样子（Codex / Claude Code 的文件视图）：一棵文件树 + 看文件。书、章、资料分类这些写小说的说法都不在这里，
 * 章节就是文件夹里的 .md 文件。改完「保存」走修改记录（和 Agent 改的一样能撤回），不另起一套保存。
 */
export function FilesPanel({
  projectId,
  snapshot,
  openPath,
  focus,
  onOpenPath,
  onSaved,
  onQuote,
  notify,
}: {
  projectId: string
  snapshot: ProjectInspection
  /** 正开着的文件（相对路径）；null 就是看文件树。 */
  openPath: string | null
  focus?: FileFocus
  onOpenPath: (path: string | null, location?: FileLocation) => void
  /** 存完以后：外面刷新文件夹快照（修改记录、对话里的「开着哪个文件」都跟着新版本走）。 */
  onSaved: () => void
  onQuote?: (quote: FileQuotation) => void
  notify: Notify
}) {
  // 在右栏内切文件、返回文件树时保留各自的草稿，连同开始编辑时的版本一起留着。
  const drafts = useRef(new Map<string, FileDraftState>())
  const [, updateDrafts] = useState(0)
  const openFile = useMemo(
    () => (openPath ? snapshot.files.find((file) => file.relative_path === openPath) ?? null : null),
    [openPath, snapshot.files],
  )
  if (openFile) {
    const key = fileDraftKey(projectId, openFile.relative_path)
    if (!drafts.current.has(key)) drafts.current.set(key, readFileDraft(key))
    const saved = drafts.current.get(key)!
    return <FileView key={key} projectId={projectId} file={openFile} focus={focus} initialDraft={saved.draft ?? undefined} draftProblem={saved.problem}
      onDraft={(draft, submitted) => {
        const previous = drafts.current.get(key)!
        // A 保存途中切走又回来编辑了新内容，旧保存完成只清它自己那份草稿。
        if (submitted && !sameFileDraft(previous.draft, submitted)) return
        // 另一页面已经保存了更新的草稿时，迟到的保存不删除那一份。
        const stored = readFileDraft(key)
        // Compare the last successfully stored identity, including when a later
        // edit failed to persist (quota). Its earlier copy still belongs to us.
        const keepStored = submitted && stored.draft && stored.storageId !== previous.storageId
        const problem = keepStored ? null : writeFileDraft(key, draft)
        const storageId = keepStored || problem ? previous.storageId : readFileDraft(key).storageId
        drafts.current.set(key, { draft, problem, storageId })
        updateDrafts((value) => value + 1)
      }}
      onOpenFile={(path) => {
        const parent = openFile.relative_path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
        const absolute = /^(?:[a-z]:[\\/]|\/|~\/)/i.test(path)
        const candidate = resolveFileNavigation(snapshot, absolute ? path : `${parent}/${path}`)
        const target = snapshot.files.some((item) => item.relative_path === candidate?.relativePath) ? candidate : resolveFileNavigation(snapshot, path) ?? candidate
        if (target?.location) onOpenPath(target.relativePath, target.location)
        else if (target) onOpenPath(target.relativePath)
        else notify('warn', `这个链接暂时无法在当前文件夹打开：${path}`)
      }}
      onBack={() => onOpenPath(null)} onSaved={onSaved} onQuote={onQuote} notify={notify} />
  }
  if (openPath) return <div className="zm-files">
    <p className="zm-muted" role="status">文件列表里暂时找不到：{openPath}</p>
    <p className="zm-muted">文件可能刚刚新建、移动或删除，刷新文件列表后再试。</p>
    <Btn size="sm" onClick={onSaved}>刷新文件列表</Btn>
    <Btn size="sm" variant="ghost" onClick={() => onOpenPath(null)}>{A.files.back}</Btn>
  </div>
  return <FileTree files={snapshot.files} onOpen={(file) => onOpenPath(file.relative_path)} />
}

// ============================================================
// 文件树
// ============================================================

interface TreeFolder {
  name: string
  path: string
  folders: TreeFolder[]
  files: ProjectFile[]
}

const collator = new Intl.Collator('zh-Hans-CN', { numeric: true, sensitivity: 'base' })

/** 快照里的文件（相对路径）拼成一棵树：文件夹在前、文件在后，按名字排（第 2 章排在第 10 章前面）。 */
export function buildFileTree(files: ProjectFile[]): TreeFolder {
  const root: TreeFolder = { name: '', path: '', folders: [], files: [] }
  for (const file of files) {
    const parts = file.relative_path.split(/[\\/]/).filter(Boolean)
    let folder = root
    for (const part of parts.slice(0, -1)) {
      const path = folder.path ? `${folder.path}/${part}` : part
      let next = folder.folders.find((item) => item.name === part)
      if (!next) {
        next = { name: part, path, folders: [], files: [] }
        folder.folders.push(next)
      }
      folder = next
    }
    folder.files.push(file)
  }
  const sort = (folder: TreeFolder) => {
    folder.folders.sort((a, b) => collator.compare(a.name, b.name))
    folder.files.sort((a, b) => collator.compare(baseName(a.relative_path), baseName(b.relative_path)))
    folder.folders.forEach(sort)
  }
  sort(root)
  return root
}

function baseName(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean)
  return parts[parts.length - 1] ?? path
}

function FileTree({ files, onOpen }: { files: ProjectFile[]; onOpen: (file: ProjectFile) => void }) {
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const tree = useMemo(() => buildFileTree(files), [files])
  const matches = useMemo(() => {
    const words = query.trim().toLowerCase().split(/\s+/).filter(Boolean)
    if (words.length === 0) return null
    return files
      .filter((file) => words.every((word) => file.relative_path.toLowerCase().includes(word)))
      .sort((a, b) => collator.compare(a.relative_path, b.relative_path))
  }, [files, query])

  const toggle = (path: string) => setExpanded((current) => {
    const next = new Set(current)
    if (next.has(path)) next.delete(path)
    else next.add(path)
    return next
  })

  return (
    <div className="zm-files">
      <input
        className="zm-files-search"
        type="search"
        value={query}
        placeholder={A.files.search}
        aria-label={A.files.search}
        onChange={(event) => setQuery(event.target.value)}
      />
      {files.length === 0 ? <p className="zm-muted">{A.files.empty}</p> : null}
      {matches ? (
        matches.length === 0 ? <p className="zm-muted">{A.files.noMatch}</p> : (
          <ul className="zm-tree" role="list">
            {matches.map((file) => (
              <li key={file.file_id}>
                <button type="button" className="zm-tree-row is-file" onClick={() => onOpen(file)} title={file.relative_path}>
                  <FileText aria-hidden />
                  <span className="zm-tree-name">{file.relative_path}</span>
                </button>
              </li>
            ))}
          </ul>
        )
      ) : (
        <ul className="zm-tree" role="list">
          <FolderRows folder={tree} depth={0} expanded={expanded} onToggle={toggle} onOpen={onOpen} />
        </ul>
      )}
    </div>
  )
}

function FolderRows({ folder, depth, expanded, onToggle, onOpen }: {
  folder: TreeFolder
  depth: number
  expanded: Set<string>
  onToggle: (path: string) => void
  onOpen: (file: ProjectFile) => void
}) {
  const indent = { paddingLeft: `${8 + depth * 14}px` }
  return (
    <>
      {folder.folders.map((child) => {
        const open = expanded.has(child.path)
        return (
          <li key={child.path}>
            <button type="button" className="zm-tree-row is-folder" style={indent} aria-expanded={open} onClick={() => onToggle(child.path)}>
              {open ? <ChevronDown aria-hidden /> : <ChevronRight aria-hidden />}
              <Folder aria-hidden />
              <span className="zm-tree-name">{child.name}</span>
            </button>
            {open ? (
              <ul className="zm-tree" role="list">
                <FolderRows folder={child} depth={depth + 1} expanded={expanded} onToggle={onToggle} onOpen={onOpen} />
              </ul>
            ) : null}
          </li>
        )
      })}
      {folder.files.map((file) => (
        <li key={file.file_id}>
          <button type="button" className="zm-tree-row is-file" style={indent} onClick={() => onOpen(file)} title={file.relative_path}>
            <span className="zm-tree-spacer" aria-hidden />
            <FileText aria-hidden />
            <span className="zm-tree-name">{baseName(file.relative_path)}</span>
          </button>
        </li>
      ))}
    </>
  )
}

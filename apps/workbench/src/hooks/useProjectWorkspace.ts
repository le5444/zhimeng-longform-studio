import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  inspectBoundProject,
  inspectProjectRoot,
  importTextProject,
  listProjectBindings,
  readBoundProjectFile,
  type ProjectBinding,
  type ProjectChapter,
  type ProjectFileContent,
  type ProjectInspection,
} from '../api/projects'
import { RuntimeApiError } from '../api/runtime'

export type ProjectLoadState = 'idle' | 'loading' | 'ready' | 'unbound' | 'error'
export type ContentLoadState = 'idle' | 'loading' | 'ready' | 'error'

// 作者上次打开的是哪本书。放 localStorage，刷新后仍回到同一本。
const SELECTED_PROJECT_STORAGE_KEY = 'zhimeng.workbench.selectedProjectId'

function readStoredProjectId(): string | null {
  try {
    return window.localStorage.getItem(SELECTED_PROJECT_STORAGE_KEY)
  } catch {
    return null
  }
}

function writeStoredProjectId(projectId: string | null): void {
  try {
    if (projectId === null) {
      window.localStorage.removeItem(SELECTED_PROJECT_STORAGE_KEY)
    } else {
      window.localStorage.setItem(SELECTED_PROJECT_STORAGE_KEY, projectId)
    }
  } catch {
    // localStorage 不可用时只影响"记住上次打开的书"，不影响主流程
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const envelope = error.payload as {
      error?: { message?: unknown }
    } | null
    if (typeof envelope?.error?.message === 'string') {
      return envelope.error.message
    }
  }
  return error instanceof Error ? error.message : '项目读取失败'
}

async function decodeTextFile(file: File): Promise<string> {
  const bytes = await file.arrayBuffer()
  for (const encoding of ['utf-8', 'gb18030']) {
    try {
      return new TextDecoder(encoding, { fatal: true }).decode(bytes)
    } catch {
      // Try the next common Chinese TXT encoding.
    }
  }
  throw new Error('TXT 编码无法识别，请使用 UTF-8 或 GB18030')
}

function preferredChapter(chapters: ProjectChapter[]): string | null {
  const proseChapters = chapters.filter((chapter) => chapter.prose_file_ids.length > 0)
  const source = proseChapters.length > 0 ? proseChapters : chapters
  if (source.length === 0) return null

  const volumeNumber = (chapter: ProjectChapter) =>
    chapter.volume_id === null
      ? Number.POSITIVE_INFINITY
      : Number.parseInt(chapter.volume_id.slice(3), 10)
  return source.reduce((latest, candidate) => {
    const volumeDelta = volumeNumber(candidate) - volumeNumber(latest)
    if (volumeDelta !== 0) return volumeDelta > 0 ? candidate : latest
    return candidate.number > latest.number ? candidate : latest
  }).chapter_id
}

export function useProjectWorkspace(runtimeReady: boolean) {
  const [state, setState] = useState<ProjectLoadState>('idle')
  const [snapshot, setSnapshot] = useState<ProjectInspection | null>(null)
  const [activeChapterId, setActiveChapterId] = useState<string | null>(null)
  const [content, setContent] = useState<ProjectFileContent | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [contentState, setContentState] = useState<ContentLoadState>('idle')
  const [contentError, setContentError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  // 书架：Runtime 里已登记的全部作品，以及作者当前打开的那一本
  const [bindings, setBindings] = useState<ProjectBinding[]>([])
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(readStoredProjectId)

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  const openBook = useCallback((projectId: string) => {
    writeStoredProjectId(projectId)
    setSelectedProjectId(projectId)
  }, [])

  const closeBook = useCallback(() => {
    writeStoredProjectId(null)
    setSelectedProjectId(null)
    setSnapshot(null)
    setActiveChapterId(null)
    setContent(null)
    setContentState('idle')
    setState('unbound')
  }, [])

  /** 导入作者选择的 TXT，由 Runtime 在受管目录内自动建立小说项目。 */
  const addBook = useCallback(async (file: File) => {
    if (file.name.toLocaleLowerCase('zh-CN').endsWith('.txt') === false) {
      throw new Error('请选择 TXT 作品文件')
    }
    const content = await decodeTextFile(file)
    const imported = await importTextProject(file.name, content)
    const inspection = imported.inspection
    const list = await listProjectBindings()
    setBindings(list)
    writeStoredProjectId(inspection.project.project_id)
    setSelectedProjectId(inspection.project.project_id)
    return inspection
  }, [])

  useEffect(() => {
    if (!runtimeReady) {
      // Keep the last verified project snapshot visible during short Runtime
      // health flaps.  Dropping it here makes an author click in Sources /
      // Knowledge suddenly fall back to the demo workspace even though the
      // Runtime status endpoint and project APIs may still be serving.
      setState((current) => (current === 'ready' ? current : 'idle'))
      setError(null)
      setContentError(null)
      return
    }

    let cancelled = false
    const load = async () => {
      setState('loading')
      setError(null)
      setContentError(null)
      try {
        const configuredRoot = (import.meta.env.VITE_PROJECT_ROOT ?? '').trim()
        let nextSnapshot: ProjectInspection | null = null

        // 书架始终要拿到完整列表，作者才能看到自己所有的书。
        const shelf = await listProjectBindings()
        if (cancelled) return
        setBindings(shelf)

        if (configuredRoot) {
          // 显式配置的项目根优先，保持既有行为不变。
          nextSnapshot = await inspectProjectRoot(configuredRoot)
        } else if (selectedProjectId !== null) {
          if (shelf.some((binding) => binding.project_id === selectedProjectId)) {
            nextSnapshot = await inspectBoundProject(selectedProjectId)
          } else {
            // 上次打开的书已经不在书架上（被删除或换机器），回到书架而不是报错。
            writeStoredProjectId(null)
            setSelectedProjectId(null)
          }
        }
        if (cancelled) return
        if (nextSnapshot === null) {
          setSnapshot(null)
          setActiveChapterId(null)
          setContent(null)
          setContentState('idle')
          setState('unbound')
          return
        }
        setSnapshot(nextSnapshot)
        setActiveChapterId((current) =>
          nextSnapshot.chapters.some((chapter) => chapter.chapter_id === current)
            ? current
            : preferredChapter(nextSnapshot.chapters),
        )
        setState('ready')
      } catch (loadError) {
        if (cancelled) return
        setSnapshot(null)
        setActiveChapterId(null)
        setContent(null)
        setError(errorMessage(loadError))
        setContentState('idle')
        setState('error')
      }
    }
    void load()
    return () => {
      cancelled = true
    }
  }, [refreshKey, runtimeReady, selectedProjectId])

  const activeChapter = useMemo(
    () =>
      snapshot?.chapters.find((chapter) => chapter.chapter_id === activeChapterId) ??
      null,
    [activeChapterId, snapshot],
  )

  useEffect(() => {
    const proseFileId = activeChapter?.prose_file_ids[0]
    if (!snapshot || !proseFileId) {
      setContent(null)
      setContentState('idle')
      setContentError(null)
      return
    }

    let cancelled = false
    setContent(null)
    setContentState('loading')
    setContentError(null)
    void readBoundProjectFile(snapshot.project.project_id, proseFileId)
      .then((nextContent) => {
        if (cancelled) return
        if (
          nextContent.project_id !== snapshot.project.project_id ||
          nextContent.snapshot_revision !== snapshot.project.revision ||
          nextContent.project_fingerprint !== snapshot.project.fingerprint
        ) {
          setContentError('项目在读取正文期间发生变化，请刷新快照')
          setContentState('error')
          return
        }
        setContent(nextContent)
        setContentState('ready')
      })
      .catch((loadError) => {
        if (!cancelled) {
          setContentError(errorMessage(loadError))
          setContentState('error')
        }
      })
    return () => {
      cancelled = true
    }
  }, [activeChapter, snapshot])

  return {
    state,
    snapshot,
    activeChapter,
    activeChapterId,
    content,
    error,
    contentState,
    contentError,
    setActiveChapterId,
    refresh,
    // 书架
    bindings,
    selectedProjectId,
    openBook,
    closeBook,
    addBook,
  }
}

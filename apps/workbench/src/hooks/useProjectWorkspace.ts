import { useCallback, useEffect, useRef, useState } from 'react'
import {
  inspectBoundProject,
  inspectProjectRoot,
  inspectProjectRootWithProgress,
  listProjectBindings,
  updateProjectDisplayName,
  type ProjectBinding,
  type ProjectInspection,
  type ScanProgress,
} from '../api/projects'
import { RuntimeApiError } from '../api/runtime'
import { addTrustedRoot } from '../api/trustedRoots'

export type ProjectLoadState = 'idle' | 'loading' | 'ready' | 'unbound' | 'error'

// 上次打开的是哪个项目（文件夹）。放 localStorage，刷新后仍回到同一个。
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
    // localStorage 不可用时只影响「记住上次打开的项目」，不影响主流程
  }
}

function errorMessage(error: unknown): string {
  if (error instanceof DOMException && error.name === 'AbortError') {
    return '读取这个文件夹被中断，请重新打开'
  }
  if (error instanceof RuntimeApiError) {
    const envelope = error.payload as {
      error?: { message?: unknown }
    } | null
    if (typeof envelope?.error?.message === 'string') {
      return envelope.error.message
    }
  }
  if (error instanceof Error && /aborted|abort/i.test(error.message)) {
    return '读取这个文件夹被中断，请重新打开'
  }
  return error instanceof Error ? error.message : '这个文件夹没读出来'
}

/**
 * 打开的项目（文件夹）：登记过的项目列表、现在打开哪一个、它的快照。
 *
 * 9-29 回炉重造：以前这里还管书架上的建书 / 删书 / 导入 TXT、章节的新建 / 改名 / 复制 / 删除、
 * 读当前章正文给章节编辑器——那些界面都删了，这里只留普通 agent 用得上的几样。
 * 以前这里还记着「开着的那一章」（打开一本书就把最新一章当成开着，发给模型的也是章号）；
 * 现在开着哪个文件归右栏管，按文件路径算，这里不再有章。
 */
export function useProjectWorkspace(runtimeReady: boolean) {
  const [state, setState] = useState<ProjectLoadState>('idle')
  const [snapshot, setSnapshot] = useState<ProjectInspection | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshKey, setRefreshKey] = useState(0)
  const [bindings, setBindings] = useState<ProjectBinding[]>([])
  const [scanProgress, setScanProgress] = useState<ScanProgress | null>(null)
  const [selectedProjectId, setSelectedProjectId] = useState<string | null>(readStoredProjectId)
  const selectedProjectIdRef = useRef(selectedProjectId)

  useEffect(() => {
    selectedProjectIdRef.current = selectedProjectId
  }, [selectedProjectId])

  const refresh = useCallback(() => setRefreshKey((value) => value + 1), [])

  const openBook = useCallback((projectId: string) => {
    writeStoredProjectId(projectId)
    setSelectedProjectId(projectId)
  }, [])

  const closeBook = useCallback(() => {
    writeStoredProjectId(null)
    setSelectedProjectId(null)
    setSnapshot(null)
    setState('unbound')
  }, [])

  /**
   * 打开电脑上一个已有的文件夹当项目。用户亲手选的就算信任：先加进受信根（以前受信根外的文件夹一律 403），
   * 再读一遍、登记。只读取与登记，不复制也不改写原文件。
   */
  const openFolder = useCallback(async (rootPath: string) => {
    setScanProgress(null)
    // Windows 的「复制文件地址」带引号，用户不该为这个报错。
    const trimmed = rootPath.trim().replace(/^"+|"+$/g, '').trim()
    if (trimmed === '') {
      throw new Error('请填写文件夹的完整路径')
    }
    try {
      await addTrustedRoot(trimmed)
      const inspection = await inspectProjectRootWithProgress(trimmed, setScanProgress)
      const list = await listProjectBindings()
      setBindings(list)
      writeStoredProjectId(inspection.project.project_id)
      setSelectedProjectId(inspection.project.project_id)
      return inspection
    } finally {
      setScanProgress(null)
    }
  }, [])

  /** 改项目的显示名（电脑上的文件夹不动）。 */
  const renameBook = useCallback(async (projectId: string, newName: string) => {
    await updateProjectDisplayName(projectId, newName)
    const list = await listProjectBindings()
    setBindings(list)
    if (selectedProjectIdRef.current === projectId) refresh()
  }, [refresh])

  useEffect(() => {
    if (!runtimeReady) {
      // 本机服务短暂掉线时留着上一次读到的快照，不让界面一闪变空。
      setState((current) => (current === 'ready' ? current : 'idle'))
      setError(null)
      return
    }

    let cancelled = false
    const controller = new AbortController()
    const load = async () => {
      setState('loading')
      setError(null)
      try {
        const configuredRoot = (import.meta.env.VITE_PROJECT_ROOT ?? '').trim()
        let nextSnapshot: ProjectInspection | null = null
        const requestOptions = { signal: controller.signal }
        const list = await listProjectBindings(requestOptions)
        if (cancelled) return
        setBindings(list)

        if (selectedProjectId !== null && list.some((binding) => binding.project_id === selectedProjectId)) {
          nextSnapshot = await inspectBoundProject(selectedProjectId, requestOptions)
        } else if (configuredRoot) {
          // 显式配置的项目根作为还没选时的默认项目。
          nextSnapshot = await inspectProjectRoot(configuredRoot, requestOptions)
          const listAfterBind = await listProjectBindings(requestOptions)
          if (!cancelled) setBindings(listAfterBind)
        } else if (selectedProjectId !== null) {
          // 上次打开的项目已经不在了（移除了或换了电脑）：回到没打开文件夹，不报错。
          writeStoredProjectId(null)
          setSelectedProjectId(null)
        }
        if (cancelled) return
        if (nextSnapshot === null) {
          setSnapshot(null)
          setState('unbound')
          return
        }
        setSnapshot(nextSnapshot)
        setState('ready')
      } catch (loadError) {
        if (cancelled) return
        setSnapshot(null)
        setError(errorMessage(loadError))
        setState('error')
      }
    }
    void load()
    return () => {
      cancelled = true
      controller.abort()
    }
  }, [refreshKey, runtimeReady, selectedProjectId])

  return {
    state,
    snapshot,
    error,
    refresh,
    bindings,
    selectedProjectId,
    openBook,
    closeBook,
    scanProgress,
    openFolder,
    renameBook,
  }
}

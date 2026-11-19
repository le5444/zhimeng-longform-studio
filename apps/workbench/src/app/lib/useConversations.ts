/**
 * 左栏那棵树的数据：界面上只有两类东西，不混为一谈。
 *
 * - **独立对话**：不绑任何项目的纯聊天。问思路、闲聊、零散的活儿。各自独立，互不共享。
 * - **项目**：一个项目（文件夹）就是一个长期容器，下面挂着属于它的对话。一个项目 → 多条对话。
 *
 * 一条对话有没有「所属的项目」决定它是哪一类：为空就是独立对话，非空就归到那个项目下。
 */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { deleteAgentSession, listAllAgentSessions, rebindAgentSessionProject, updateAgentSession, type AgentSession } from '../../api/agent'
import type { ProjectBinding } from '../../api/projects'
import { displayProjectName } from '../../lib/bookshelfProjects'

export type LoadState = 'idle' | 'loading' | 'ready' | 'error'

/** 一条对话在左栏里的样子。 */
export interface ConversationRow {
  sessionId: string
  title: string
  /** 属于哪个项目；null 就是独立对话。 */
  projectId: string | null
  pinned: boolean
  updatedAt: string
  /** 一句话都没说过的对话：左栏不列它（正开着的那条除外），免得一排「新对话」占位。 */
  empty: boolean
  running?: boolean
}

/** 左栏「项目」那一段里的一个项目。 */
export interface ProjectConversationGroup {
  projectId: string
  name: string
  /** 这个项目的文件夹在电脑上的位置（左栏悬停时给看）。 */
  rootPath?: string
  conversations: ConversationRow[]
  /** 项目名下面那一行：最近一条对话的标题。 */
  latest: string | null
}

export interface ConversationsApi {
  state: LoadState
  error: string | null
  /** 不绑项目的纯对话。 */
  loose: ConversationRow[]
  /** 每个项目和它名下的对话。 */
  projects: ProjectConversationGroup[]
  byId: ReadonlyMap<string, ConversationRow>
  refresh: () => void
  /** 换这条对话所属的项目；传 null 就变回纯对话。 */
  rebind: (sessionId: string, projectId: string | null) => Promise<void>
  /** 置顶 / 取消置顶一条对话；置顶的排在最前面。 */
  togglePin: (sessionId: string, pinned: boolean) => Promise<void>
  /** 给一条对话改名。 */
  rename: (sessionId: string, title: string) => Promise<void>
  /** 删掉一条对话（后台是关上不再列出，项目里的内容不受影响）。 */
  remove: (sessionId: string) => Promise<void>
}

function toRow(session: AgentSession): ConversationRow {
  return {
    sessionId: session.session_id,
    title: session.title.trim(),
    projectId: session.project_id ?? session.project_revision?.project_id ?? null,
    pinned: Boolean(session.pinned),
    updatedAt: session.updated_at,
    empty: session.last_message_ordinal === 0 && session.last_run_ordinal === 0,
    running: session.status === 'active' && session.last_run_ordinal > 0,
  }
}

function newest(left: ConversationRow, right: ConversationRow): number {
  if (left.pinned !== right.pinned) return left.pinned ? -1 : 1
  return Date.parse(right.updatedAt) - Date.parse(left.updatedAt)
}

export function useConversations(enabled: boolean, bindings: ProjectBinding[]): ConversationsApi {
  const [rows, setRows] = useState<ConversationRow[]>([])
  const [state, setState] = useState<LoadState>('idle')
  const [error, setError] = useState<string | null>(null)
  const [tick, setTick] = useState(0)
  const refresh = useCallback(() => setTick((value) => value + 1), [])
  const scope = useMemo(() => ({ enabled }), [enabled])
  const currentScope = useRef(scope)
  currentScope.current = scope
  const revision = useRef(0)
  const pending = useRef(new Map<string, Promise<void>>())
  const mounted = useRef(true)
  useEffect(() => {
    mounted.current = true
    return () => { mounted.current = false }
  }, [])

  useEffect(() => {
    if (!enabled) {
      // Connectivity is not deletion. Keep the last list until a successful read.
      setError(null)
      setState('idle')
      return
    }
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined
    const requestedRevision = revision.current
    setState('loading')
    listAllAgentSessions()
      .then((sessions) => {
        if (cancelled) return
        // 列表请求先出发、改名/删除后完成时，旧列表不能复活删除项或覆盖新名字。
        if (revision.current !== requestedRevision) { refresh(); return }
        setRows(sessions.filter((item) => item.status !== 'closed').map(toRow).sort(newest))
        setError(null)
        setState('ready')
      })
      .catch((caught: unknown) => {
        if (cancelled) return
        setError(caught instanceof Error ? caught.message : String(caught))
        setState('error')
      })
      .finally(() => {
        if (!cancelled) timer = setTimeout(refresh, 3000)
      })
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [enabled, tick, refresh])

  useEffect(() => {
    if (!enabled) return
    window.addEventListener('focus', refresh)
    return () => window.removeEventListener('focus', refresh)
  }, [enabled, refresh])

  // 同一条对话的快速连续操作串行发送，防止较旧的完整 session 响应覆盖后来改名/置顶。
  const mutate = useCallback((sessionId: string, action: () => Promise<AgentSession | void>) => {
    const operation = (pending.current.get(sessionId) ?? Promise.resolve()).catch(() => undefined).then(async () => {
      if (!mounted.current || currentScope.current !== scope || !scope.enabled) return
      const updated = await action()
      if (!mounted.current || currentScope.current !== scope) return
      revision.current += 1
      setRows((current) => updated
        ? current.map((item) => item.sessionId === sessionId ? toRow(updated) : item).sort(newest)
        : current.filter((item) => item.sessionId !== sessionId))
    })
    pending.current.set(sessionId, operation)
    void operation.finally(() => {
      if (pending.current.get(sessionId) === operation) pending.current.delete(sessionId)
    }).catch(() => undefined)
    return operation
  }, [scope])

  const rebind = useCallback(async (sessionId: string, projectId: string | null) => {
    await mutate(sessionId, () => rebindAgentSessionProject(sessionId, projectId))
  }, [mutate])

  const togglePin = useCallback(async (sessionId: string, pinned: boolean) => {
    await mutate(sessionId, () => updateAgentSession(sessionId, { pinned }))
  }, [mutate])

  const rename = useCallback(async (sessionId: string, title: string) => {
    await mutate(sessionId, () => updateAgentSession(sessionId, { title: title.trim() }))
  }, [mutate])

  const remove = useCallback(async (sessionId: string) => {
    await mutate(sessionId, async () => { await deleteAgentSession(sessionId) })
  }, [mutate])

  const loose = useMemo(() => rows.filter((item) => item.projectId === null), [rows])

  const projects = useMemo(() => {
    const grouped = new Map<string, ConversationRow[]>()
    for (const row of rows) {
      if (row.projectId === null) continue
      const list = grouped.get(row.projectId) ?? []
      list.push(row)
      grouped.set(row.projectId, list)
    }
    return bindings.map((binding) => {
      const conversations = (grouped.get(binding.project_id) ?? []).sort(newest)
      return {
        projectId: binding.project_id,
        name: displayProjectName(binding),
        rootPath: binding.root_path,
        conversations,
        latest: conversations[0]?.title ?? null,
      }
    })
  }, [bindings, rows])

  const byId = useMemo(() => new Map(rows.map((row) => [row.sessionId, row])), [rows])

  return useMemo(
    () => ({ state, error, loose, projects, byId, refresh, rebind, togglePin, rename, remove }),
    [projects, byId, error, loose, rebind, refresh, remove, rename, state, togglePin],
  )
}

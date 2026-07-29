import { useCallback, useEffect, useRef, useState } from 'react'
import {
  rebuildProjectSearchIndex,
  searchProject,
  type ProjectSearchAuthority,
  type ProjectSearchHit,
  type ProjectSearchIndexManifest,
  type ProjectSearchQueryResponse,
  type ProjectSearchSourceType,
} from '../api/projectSearch'
import { RuntimeApiError } from '../api/runtime'

export type ProjectSearchLoadState = 'idle' | 'loading' | 'ready' | 'error'

function searchErrorMessage(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: unknown }
      detail?: unknown
    } | null
    if (typeof payload?.error?.message === 'string') return payload.error.message
    if (typeof payload?.detail === 'string') return payload.detail
    return `检索失败（HTTP ${error.status}）`
  }
  if (error instanceof DOMException && error.name === 'AbortError') {
    return '检索超时，请重试'
  }
  return error instanceof Error ? error.message : '项目检索失败'
}

export function useProjectSearch(projectId: string | null, enabled = true) {
  const [query, setQueryValue] = useState('')
  const [sourceType, setSourceTypeValue] = useState<ProjectSearchSourceType | 'all'>('all')
  const [authority, setAuthorityValue] = useState<ProjectSearchAuthority | 'all'>('all')
  const [result, setResult] = useState<ProjectSearchQueryResponse | null>(null)
  const [hits, setHits] = useState<ProjectSearchHit[]>([])
  const [searchState, setSearchState] = useState<ProjectSearchLoadState>('idle')
  const [searchError, setSearchError] = useState<string | null>(null)
  const [rebuildState, setRebuildState] = useState<ProjectSearchLoadState>('idle')
  const [rebuildError, setRebuildError] = useState<string | null>(null)
  const [manifest, setManifest] = useState<ProjectSearchIndexManifest | null>(null)
  const searchEpoch = useRef(0)
  const rebuildEpoch = useRef(0)

  useEffect(() => {
    searchEpoch.current += 1
    rebuildEpoch.current += 1
    setQueryValue('')
    setSourceTypeValue('all')
    setAuthorityValue('all')
    setResult(null)
    setHits([])
    setSearchState('idle')
    setSearchError(null)
    setRebuildState('idle')
    setRebuildError(null)
    setManifest(null)
  }, [projectId])

  const setQuery = useCallback((value: string) => {
    setQueryValue(value)
  }, [])

  const setSourceType = useCallback((value: ProjectSearchSourceType | 'all') => {
    setSourceTypeValue(value)
  }, [])

  const setAuthority = useCallback((value: ProjectSearchAuthority | 'all') => {
    setAuthorityValue(value)
  }, [])

  const runSearch = useCallback(async () => {
    if (!enabled || !projectId) return
    const normalized = query.trim()
    if (!normalized) {
      setSearchError('请输入检索词')
      setSearchState('error')
      return
    }
    const epoch = ++searchEpoch.current
    setSearchState('loading')
    setSearchError(null)
    try {
      const response = await searchProject(projectId, {
        query: normalized,
        source_types: sourceType === 'all' ? undefined : [sourceType],
        authorities: authority === 'all' ? undefined : [authority],
        limit: 30,
        auto_rebuild: true,
      })
      if (searchEpoch.current !== epoch) return
      setResult(response)
      setHits(response.hits)
      setManifest((current) => current && current.index_hash === response.index_hash
        ? current
        : current)
      setSearchState('ready')
    } catch (error) {
      if (searchEpoch.current !== epoch) return
      setResult(null)
      setHits([])
      setSearchError(searchErrorMessage(error))
      setSearchState('error')
    }
  }, [authority, enabled, projectId, query, sourceType])

  const rebuild = useCallback(async (force = true) => {
    if (!enabled || !projectId) return
    const epoch = ++rebuildEpoch.current
    setRebuildState('loading')
    setRebuildError(null)
    try {
      const response = await rebuildProjectSearchIndex(projectId, { force })
      if (rebuildEpoch.current !== epoch) return
      setManifest(response.manifest)
      setRebuildState('ready')
      if (query.trim()) {
        await runSearch()
      }
    } catch (error) {
      if (rebuildEpoch.current !== epoch) return
      setRebuildError(searchErrorMessage(error))
      setRebuildState('error')
    }
  }, [enabled, projectId, query, runSearch])

  return {
    query,
    setQuery,
    sourceType,
    setSourceType,
    authority,
    setAuthority,
    result,
    hits,
    searchState,
    searchError,
    rebuildState,
    rebuildError,
    manifest,
    runSearch,
    rebuild,
    busy: searchState === 'loading' || rebuildState === 'loading',
  }
}

export type ProjectSearchState = ReturnType<typeof useProjectSearch>

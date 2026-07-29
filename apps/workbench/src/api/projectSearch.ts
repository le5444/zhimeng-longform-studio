import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export const PROJECT_SEARCH_TIMEOUT_MS = 60_000

export type ProjectSearchSourceType = 'project_file' | 'formal_knowledge' | 'authorized_source'
export type ProjectSearchAuthority =
  | 'project_truth'
  | 'formal_knowledge'
  | 'reference'
  | 'risk'

export interface ProjectSearchHit {
  schema: 'zhimeng.project-search-hit.v1'
  source_id: string
  source_type: ProjectSearchSourceType
  authority: ProjectSearchAuthority
  title: string
  relative_path: string | null
  version: string
  source_hash: string
  snippet: string
  reason: string
  score: number
  updated_at: string | null
}

export interface ProjectSearchQueryRequest {
  schema?: 'zhimeng.project-search-query-request.v1'
  query: string
  source_types?: ProjectSearchSourceType[]
  authorities?: ProjectSearchAuthority[]
  updated_after?: string
  updated_before?: string
  limit?: number
  auto_rebuild?: boolean
}

export interface ProjectSearchQueryResponse {
  schema: 'zhimeng.project-search-query-response.v1'
  project_id: string
  query: string
  index_hash: string
  source_fingerprint: string
  rebuilt_before_search: boolean
  match_mode: 'full_text'
  vector_enhanced: false
  hit_count: number
  hits: ProjectSearchHit[]
}

export interface ProjectSearchRebuildRequest {
  schema?: 'zhimeng.project-search-rebuild-request.v1'
  force?: boolean
}

export interface ProjectSearchIndexManifest {
  schema: 'zhimeng.project-search-index.v1'
  project_id: string
  builder_id: 'zhimeng.project-search'
  builder_version: '1'
  source_fingerprint: string
  project_revision: number
  project_fingerprint: string
  knowledge_fingerprint: string
  document_count: number
  full_text_indexed: boolean
  vector_index_ready: false
  rebuilt_at: string
  index_hash: string
}

export interface ProjectSearchRebuildResult {
  schema: 'zhimeng.project-search-rebuild-result.v1'
  project_id: string
  rebuilt: boolean
  status: 'current' | 'rebuilt'
  manifest: ProjectSearchIndexManifest
}

function jsonRequest(method: 'POST', body: unknown): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export async function rebuildProjectSearchIndex(
  projectId: string,
  request: ProjectSearchRebuildRequest = {},
  options: RuntimeClientOptions = {},
): Promise<ProjectSearchRebuildResult> {
  const payload = {
    schema: 'zhimeng.project-search-rebuild-request.v1' as const,
    force: request.force ?? false,
  }
  return fetchRuntimeJson<ProjectSearchRebuildResult>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/search/rebuild`,
    { ...options, timeoutMs: options.timeoutMs ?? PROJECT_SEARCH_TIMEOUT_MS },
    jsonRequest('POST', payload),
  )
}

export async function searchProject(
  projectId: string,
  request: ProjectSearchQueryRequest,
  options: RuntimeClientOptions = {},
): Promise<ProjectSearchQueryResponse> {
  const payload = {
    schema: 'zhimeng.project-search-query-request.v1' as const,
    query: request.query,
    source_types: request.source_types,
    authorities: request.authorities,
    updated_after: request.updated_after,
    updated_before: request.updated_before,
    limit: request.limit ?? 20,
    auto_rebuild: request.auto_rebuild ?? true,
  }
  return fetchRuntimeJson<ProjectSearchQueryResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/search`,
    { ...options, timeoutMs: options.timeoutMs ?? PROJECT_SEARCH_TIMEOUT_MS },
    jsonRequest('POST', payload),
  )
}

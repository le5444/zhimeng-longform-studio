import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type ProjectAssetKind = 'character' | 'plot' | 'foreshadowing' | 'timeline' | 'other'

export interface ProjectAssetRecord {
  asset_id: string
  kind: ProjectAssetKind | string
  title: string
  relative_path: string
  content_hash: string
  byte_size: number
  relationship_count: number
  status_tracked: boolean
}

export interface ProjectAssetIssue {
  issue_id: string
  kind: string
  severity: string
  asset_id: string
  title: string
  detail: string
  resolved: false
}

export interface ProjectAssetRelationship {
  relationship_id: string
  from_asset_id: string
  to_asset_id: string
  relation_type: string
  confidence: string
  complete: false
}

export interface ProjectAssetIndex {
  schema: 'zhimeng.project-asset-index.v1'
  project_id: string
  project_root: string
  asset_count: number
  assets: ProjectAssetRecord[]
  relationship_count?: number
  relationships?: ProjectAssetRelationship[]
  status_tracked_count?: number
  issue_count?: number
  issue_cards?: ProjectAssetIssue[]
  domain_issue_cards_complete?: false
  relationship_graph_complete: false
  status_tracking_complete: false
  formal_draft_allowed: false
  policy_version: string
  generated_at: string
  index_id: string
  index_hash: string
}

export interface ProjectAssetIndexResponse {
  schema: 'zhimeng.project-asset-index-response.v1'
  index: ProjectAssetIndex
  not_claiming: string[]
}

function jsonRequest(method: 'GET' | 'POST', body?: unknown): RequestInit {
  if (method === 'GET') {
    return { method }
  }
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body),
  }
}

export async function rebuildProjectAssets(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectAssetIndexResponse> {
  return fetchRuntimeJson<ProjectAssetIndexResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/assets/rebuild`,
    options,
    jsonRequest('POST', {}),
  )
}

export async function getProjectAssets(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectAssetIndexResponse> {
  return fetchRuntimeJson<ProjectAssetIndexResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/assets`,
    options,
    jsonRequest('GET'),
  )
}

import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface ProjectBinding {
  project_id: string
  display_name: string
  root_path: string
  revision: number
  fingerprint: string | null
  snapshot_ready: boolean
  created_at: string
  updated_at: string
}

export interface ProjectSummary {
  project_id: string
  display_name: string
  root_path: string
  format: '01-09'
  revision: number
  fingerprint: string
}

export interface ProjectSection {
  index: number
  label: string
  present: boolean
  relative_path: string | null
}

export type ProjectFileKind =
  | 'prose'
  | 'document'
  | 'control'
  | 'handoff'
  | 'context'
  | 'review'
  | 'status'
  | 'structured_state'
  | 'asset'

export interface ProjectFile {
  file_id: string
  relative_path: string
  absolute_path: string
  section_index: number | null
  chapter_id: string | null
  volume_id: string | null
  kind: ProjectFileKind
  size_bytes: number
  sha256: string
}

export interface ProjectChapter {
  chapter_id: string
  number: number
  volume_id: string | null
  file_ids: string[]
  prose_file_ids: string[]
}

export interface ProjectVolume {
  volume_id: string
  number: number
  chapter_ids: string[]
}

export interface ProjectInspection {
  schema: 'zhimeng.project-inspection.v1'
  read_only: true
  project: ProjectSummary
  sections: ProjectSection[]
  missing_sections: number[]
  volumes: ProjectVolume[]
  chapters: ProjectChapter[]
  files: ProjectFile[]
  warnings: string[]
}

export interface TextProjectImportResult {
  schema: 'zhimeng.text-project-import-response.v1'
  source_file_name: string
  title: string
  chapter_count: number
  inspection: ProjectInspection
}

export interface ProjectFileContent {
  schema: 'zhimeng.project-file-content.v1'
  read_only: true
  project_id: string
  snapshot_revision: number
  project_fingerprint: string
  file: ProjectFile
  etag: string
  content: string
}

interface ProjectBindingList {
  schema: 'zhimeng.project-bindings.v1'
  projects: ProjectBinding[]
}

/**
 * 扫描项目要遍历整个小说文件夹，长篇会很慢：
 * 974 章 / 7533 文件的书实测要 12.8 秒，远超默认的 3.5 秒超时。
 * 不放宽的话，作者最大的那本书会直接打不开（界面退回示例态，且没有任何报错）。
 */
const PROJECT_SCAN_TIMEOUT_MS = 120_000

function jsonRequest(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export async function listProjectBindings(
  options: RuntimeClientOptions = {},
): Promise<ProjectBinding[]> {
  const payload = await fetchRuntimeJson<ProjectBindingList>(
    '/api/v1/projects',
    options,
  )
  return payload.projects
}

export function inspectProjectRoot(
  rootPath: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectInspection> {
  return fetchRuntimeJson(
    '/api/v1/projects/inspect',
    { timeoutMs: PROJECT_SCAN_TIMEOUT_MS, ...options },
    jsonRequest({ root_path: rootPath }),
  )
}

export function importTextProject(
  fileName: string,
  content: string,
  options: RuntimeClientOptions = {},
): Promise<TextProjectImportResult> {
  return fetchRuntimeJson(
    '/api/v1/projects/import-text',
    { timeoutMs: PROJECT_SCAN_TIMEOUT_MS, ...options },
    jsonRequest({
      schema: 'zhimeng.text-project-import-request.v1',
      file_name: fileName,
      content,
    }),
  )
}

export function inspectBoundProject(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectInspection> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/inspect`,
    { timeoutMs: PROJECT_SCAN_TIMEOUT_MS, ...options },
  )
}

export function readBoundProjectFile(
  projectId: string,
  fileId: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectFileContent> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/files/${encodeURIComponent(fileId)}`,
    { timeoutMs: PROJECT_SCAN_TIMEOUT_MS, ...options },
  )
}

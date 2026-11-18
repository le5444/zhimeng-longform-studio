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
  format: '01-09' | 'folder'
  creation_mode?: 'manual' | 'agent'
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

export interface ProjectFileContent {
  schema: 'zhimeng.project-file-content.v1'
  read_only: true
  project_id: string
  snapshot_revision: number
  project_fingerprint: string
  file: ProjectFile
  etag: string
  content: string
  extraction?: {
    format: 'docx' | 'xlsx' | 'docm' | 'xlsm'
    parser: 'openxml-text-v1'
    read_only: true
    source_sha256: string
    source_bytes: number
    warnings: string[]
  } | null
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

function jsonRequest(body: unknown, method: string = 'POST'): RequestInit {
  return {
    method,
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

export interface ScanProgress {
  scanned: number
  total: number
}

/** 大书扫描：后台任务 + 轮询，作者能看到「扫到第几个文件」。 */
export async function createInspectJob(
  rootPath: string,
  options: RuntimeClientOptions = {},
): Promise<string> {
  const payload = await fetchRuntimeJson<{ job_id: string }>(
    '/api/v1/projects/inspect-jobs',
    options,
    jsonRequest({ root_path: rootPath }),
  )
  return payload.job_id
}

interface InspectJobState {
  status: 'queued' | 'running' | 'done' | 'failed'
  scanned: number
  total: number
  result: ProjectInspection | null
  error: string | null
}

export async function getInspectJob(
  jobId: string,
  options: RuntimeClientOptions = {},
): Promise<InspectJobState> {
  return fetchRuntimeJson<InspectJobState>(
    `/api/v1/projects/inspect-jobs/${encodeURIComponent(jobId)}`,
    { timeoutMs: 15_000, ...options },
  )
}

export async function inspectProjectRootWithProgress(
  rootPath: string,
  onProgress: (progress: ScanProgress) => void,
  options: RuntimeClientOptions = {},
): Promise<ProjectInspection> {
  const jobId = await createInspectJob(rootPath, { timeoutMs: 15_000, ...options })
  for (;;) {
    options.signal?.throwIfAborted()
    await new Promise<void>((resolve, reject) => {
      const abort = () => {
        window.clearTimeout(timer)
        options.signal?.removeEventListener('abort', abort)
        reject(options.signal?.reason ?? new DOMException('Aborted', 'AbortError'))
      }
      const timer = window.setTimeout(() => {
        options.signal?.removeEventListener('abort', abort)
        resolve()
      }, 400)
      options.signal?.addEventListener('abort', abort, { once: true })
    })
    const state = await getInspectJob(jobId, options)
    options.signal?.throwIfAborted()
    onProgress({ scanned: state.scanned, total: state.total })
    if (state.status === 'done' && state.result) return state.result
    if (state.status === 'failed') {
      throw new Error(state.error || '扫描这个文件夹没有成功，请稍后重试。')
    }
  }
}

export function updateProjectDisplayName(
  projectId: string,
  displayName: string,
  options: RuntimeClientOptions = {},
): Promise<ProjectBinding> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}`,
    options,
    jsonRequest({ display_name: displayName }, 'PATCH'),
  )
}

/** 在资源管理器里打开这个项目自己的文件夹。 */
export function revealProject(
  projectId: string,
  options: RuntimeClientOptions = {},
): Promise<{ ok: boolean; path: string }> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/reveal`,
    options,
    { method: 'POST' },
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

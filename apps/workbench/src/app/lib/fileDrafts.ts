import type { ProjectFileContent } from '../../api/projects'

export interface PendingFileSave { changeSetId: string; text: string; afterSha256: string }
export interface FileDraft { base: ProjectFileContent; text: string; pendingSave?: PendingFileSave }
export interface FileDraftState { draft: FileDraft | null; problem: string | null; storageId: string | null }

const PREFIX = 'zhimeng.workbench.file-draft:'
export function fileDraftKey(projectId: string, path: string): string {
  return JSON.stringify([projectId, path])
}

export function readFileDraft(key: string): FileDraftState {
  try {
    const raw = window.localStorage.getItem(PREFIX + key)
    if (!raw) return { draft: null, problem: null, storageId: null }
    const value = JSON.parse(raw)
    const [projectId, path] = JSON.parse(key)
    const base = value?.base
    if (value?.version !== 1 || typeof value.text !== 'string'
      || base?.schema !== 'zhimeng.project-file-content.v1'
      || base.project_id !== projectId || base.file?.relative_path !== path
      || typeof base.content !== 'string' || typeof base.file.file_id !== 'string'
      || typeof base.project_fingerprint !== 'string' || !Number.isInteger(base.snapshot_revision)) {
      return { draft: null, problem: '本机草稿格式无法读取，原记录仍保留在浏览器中。', storageId: raw }
    }
    const pendingSave = value.pendingSave
    if (pendingSave !== undefined && (typeof pendingSave?.changeSetId !== 'string'
      || !pendingSave.changeSetId || typeof pendingSave.text !== 'string' || typeof pendingSave.afterSha256 !== 'string')) {
      return { draft: null, problem: '本机草稿中的保存记录无法读取，原记录仍保留在浏览器中。', storageId: raw }
    }
    return { draft: { base, text: value.text, ...(pendingSave ? { pendingSave } : {}) }, problem: null, storageId: typeof value.storageId === 'string' ? value.storageId : raw }
  } catch {
    return { draft: null, problem: '本机草稿暂时无法读取。', storageId: null }
  }
}

export function writeFileDraft(key: string, draft: FileDraft | null): string | null {
  try {
    if (draft) window.localStorage.setItem(PREFIX + key, JSON.stringify({ version: 1, storageId: crypto.randomUUID(), ...draft }))
    else window.localStorage.removeItem(PREFIX + key)
    return null
  } catch {
    return draft ? '草稿仅保存在当前页面，浏览器未能保存副本；关闭或刷新前请保存文件或复制内容。'
      : '浏览器未能清除旧草稿，下次打开可能再次出现。'
  }
}

export function sameFileDraft(left: FileDraft | null, right: FileDraft): boolean {
  return !!left && left.text === right.text && left.base.project_fingerprint === right.base.project_fingerprint
    && left.base.snapshot_revision === right.base.snapshot_revision && left.base.file.file_id === right.base.file.file_id
}

export interface WriteResult {
  path: string
  status: 'saved' | 'unchanged' | 'unconfirmed' | 'rolled_back'
  changeId?: string
  external?: boolean
}

function object(value: unknown): Record<string, unknown> | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : null
}

/** Project and external receipts share a list, but retain their own commit state. */
export function writeResults(output: unknown): WriteResult[] {
  const root = object(output)
  if (!root) return []
  const results = new Map<string, WriteResult>()
  const key = (path: string) => path.replaceAll('\\', '/').toLowerCase()
  for (const receipt of [root, object(root.outside_open_folder)]) {
    if (!receipt) continue
    const files = Array.isArray(receipt.files) ? receipt.files : [receipt]
    for (const raw of files) {
      const file = object(raw)
      const path = file?.relative_path ?? file?.path
      if (typeof path !== 'string' || !path) continue
      const status: WriteResult['status'] = receipt.status === 'rolled_back' || file?.status === 'rolled_back' ? 'rolled_back'
        : file?.operation === 'unchanged' ? 'unchanged'
          : receipt.applied === true && ['created', 'updated', 'create', 'update', 'written'].includes(String(file?.operation)) ? 'saved' : 'unconfirmed'
      const changeId = receipt.change_set_id ?? receipt.change_id
      const external = receipt !== root || receipt.schema === 'zhimeng.tool-result.file-change.v1'
      results.set(key(path), { path, status,
        ...(typeof changeId === 'string' ? { changeId } : {}),
        ...(external ? { external: true } : {}),
      })
    }
  }
  if (Array.isArray(root.unconfirmed_paths)) for (const path of root.unconfirmed_paths) {
    if (typeof path === 'string' && path && !results.has(key(path))) results.set(key(path), {
      path, status: 'unconfirmed', ...(/^(?:[a-z]:[\\/]|\/)/i.test(path) ? { external: true } : {}),
    })
  }
  return [...results.values()]
}

import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface TrustedRootsView {
  trusted_roots: string[]
  persisted_roots: string[]
}

export function addTrustedRoot(
  path: string,
  options: RuntimeClientOptions = {},
): Promise<TrustedRootsView> {
  return fetchRuntimeJson('/api/v1/runtime/trusted-roots', options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ path }),
  })
}

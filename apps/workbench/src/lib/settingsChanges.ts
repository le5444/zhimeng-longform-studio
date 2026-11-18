/** Only invalidation notices cross windows; configuration and credentials do not. */
export type SettingsResource = 'providers' | 'mcp'
const eventName = 'zhimeng:settings-changed'
const channelName = 'zhimeng-settings'

export function settingsRevisionQuery(revision?: string, catalogRevision?: string) {
  const query = new URLSearchParams()
  if (revision) query.set('expected_revision', revision)
  if (catalogRevision) query.set('expected_catalog_revision', catalogRevision)
  return query.size ? `?${query}` : ''
}

export function publishSettingsChange(resource: SettingsResource) {
  if (typeof window === 'undefined') return
  window.dispatchEvent(new CustomEvent(eventName, { detail: resource }))
  try {
    if (typeof window.BroadcastChannel === 'function') {
      const channel = new window.BroadcastChannel(channelName)
      channel.postMessage(resource)
      channel.close()
    }
  } catch { /* Focus/polling still refreshes when browser messaging is unavailable. */ }
}

export function subscribeSettingsChanges(resource: SettingsResource, refresh: () => void) {
  const receive = (event: Event) => { if ((event as CustomEvent).detail === resource) refresh() }
  const visible = () => { if (document.visibilityState !== 'hidden') refresh() }
  window.addEventListener(eventName, receive)
  window.addEventListener('focus', visible)
  document.addEventListener('visibilitychange', visible)
  let channel: BroadcastChannel | undefined
  try {
    if (typeof window.BroadcastChannel === 'function') {
      channel = new window.BroadcastChannel(channelName)
      channel.onmessage = (event) => { if (event.data === resource) refresh() }
    }
  } catch { /* Re-read on focus even when BroadcastChannel cannot be opened. */ }
  return () => {
    window.removeEventListener(eventName, receive)
    window.removeEventListener('focus', visible)
    document.removeEventListener('visibilitychange', visible)
    channel?.close()
  }
}

export async function changedSettings<T>(resource: SettingsResource, request: Promise<T>): Promise<T> {
  const result = await request
  publishSettingsChange(resource)
  return result
}

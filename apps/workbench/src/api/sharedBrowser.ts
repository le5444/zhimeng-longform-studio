export interface SharedBrowserTab {
  id: string
  session_id: string
  url: string
  title: string
  mode: 'user' | 'agent'
  loading: boolean
  error: string | null
  restored: boolean
  can_back: boolean
  can_forward: boolean
}
export interface SharedDownload {
  id: string
  session_id: string
  name: string
  path: string
  status: string
  bytes: number
  sha256: string | null
}
export interface SharedBrowserState {
  tabs: SharedBrowserTab[]
  active: string | null
  downloads: SharedDownload[]
  error: string | null
}
export interface SharedBrowserBridge {
  state: () => Promise<SharedBrowserState>
  command: (request: Record<string, unknown>) => Promise<unknown>
  onChange: (callback: (state: SharedBrowserState) => void) => () => void
  onLayout: (callback: () => void) => () => void
}
declare global { interface Window { zhimengBrowser?: SharedBrowserBridge } }
export const sharedBrowser = () => window.zhimengBrowser

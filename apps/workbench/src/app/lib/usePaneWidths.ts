import { useEffect, useState } from 'react'

const DEFAULTS = { side: 260, panel: 440 }
const MIN = { side: 200, panel: 320 }
const MAX = { side: 360, panel: 900 }
type Pane = keyof typeof DEFAULTS
const key = (pane: Pane) => `zhimeng.app.${pane}-width`

function readWidth(pane: Pane): number {
  try {
    const value = Number(window.localStorage.getItem(key(pane)))
    return Number.isFinite(value) && value >= MIN[pane] && value <= MAX[pane] ? value : DEFAULTS[pane]
  } catch { return DEFAULTS[pane] }
}

export function usePaneWidths(panelShown: boolean, panelWide: boolean, sideCollapsed: boolean) {
  const [requested, setRequested] = useState(() => ({ side: readWidth('side'), panel: readWidth('panel') }))
  const [preview, setPreview] = useState<{ pane: Pane; value: number } | null>(null)
  const [viewport, setViewport] = useState(() => window.innerWidth)
  useEffect(() => {
    const resize = () => setViewport(window.innerWidth)
    window.addEventListener('resize', resize)
    return () => window.removeEventListener('resize', resize)
  }, [])

  // Keep the saved preference separate from a temporary, narrower window.
  const chatMin = panelWide ? 380 : 480
  const sideMax = Math.max(MIN.side, Math.min(MAX.side, viewport - chatMin - (panelShown ? MIN.panel : 0)))
  const side = sideCollapsed ? 54 : Math.min(preview?.pane === 'side' ? preview.value : requested.side, sideMax)
  const panelMax = Math.max(MIN.panel, Math.min(panelWide ? viewport : MAX.panel, viewport - side - chatMin))
  const panel = Math.max(MIN.panel, Math.min(panelWide ? (viewport - side) * 0.64 : preview?.pane === 'panel' ? preview.value : requested.panel, panelMax))

  const update = (pane: Pane, value: number, persist: boolean) => {
    const width = Math.round(Math.max(MIN[pane], Math.min(MAX[pane], value)))
    if (!persist) { setPreview({ pane, value: width }); return }
    setPreview(null)
    setRequested(current => ({ ...current, [pane]: width }))
    try { window.localStorage.setItem(key(pane), String(width)) } catch { /* Keep this window usable. */ }
  }
  return {
    side: { value: side, min: MIN.side, max: sideMax, defaultValue: DEFAULTS.side, onChange: (value: number, persist: boolean) => update('side', value, persist), onCancel: () => setPreview(null) },
    panel: { value: panel, min: MIN.panel, max: panelMax, defaultValue: DEFAULTS.panel, onChange: (value: number, persist: boolean) => update('panel', value, persist), onCancel: () => setPreview(null) },
  }
}

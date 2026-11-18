/** 界面字号（设置 → 外观）：只改一个 --zm-ui，其余字号都从它算出来（前端吸收 9-29：ZCode --ui-font-size）。 */
export type UiSize = 's' | 'm' | 'l' | 'xl'
export const DEFAULT_UI_SIZE: UiSize = 'l'
export const UI_SIZES: readonly UiSize[] = ['s', 'm', 'l', 'xl']
const PIXELS: Record<UiSize, number> = { s: 13, m: 14, l: 15, xl: 16 }
const KEY = 'zhimeng.app.ui-size'
const EVENT = 'zhimeng:ui-size'

export function readUiSize(): UiSize {
  try {
    const value = window.localStorage.getItem(KEY)
    return value === 's' || value === 'm' || value === 'l' || value === 'xl' ? value : DEFAULT_UI_SIZE
  } catch {
    return DEFAULT_UI_SIZE
  }
}

export function writeUiSize(size: UiSize): boolean {
  try {
    window.localStorage.setItem(KEY, size)
  } catch {
    return false
  }
  window.dispatchEvent(new CustomEvent(EVENT, { detail: size }))
  return true
}

export function uiSizePixels(size: UiSize): number {
  return PIXELS[size]
}

/** 根组件用：字号改了立刻跟着变。 */
export function onUiSizeChange(listener: (size: UiSize) => void): () => void {
  const handler = (event: Event) => listener((event as CustomEvent<UiSize>).detail)
  window.addEventListener(EVENT, handler)
  return () => window.removeEventListener(EVENT, handler)
}

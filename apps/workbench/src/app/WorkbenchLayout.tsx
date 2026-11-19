import type { CSSProperties, HTMLAttributes } from 'react'
import { PaneResizeHandle } from './PaneResizeHandle'
import { usePaneWidths } from './lib/usePaneWidths'

interface Props extends HTMLAttributes<HTMLDivElement> {
  sideCollapsed: boolean
  panelShown: boolean
  panelWide: boolean
  settingsOpen: boolean
}

// Resizing updates this frame without rerendering the conversation subtree.
export function WorkbenchLayout({ children, style, sideCollapsed, panelShown, panelWide, settingsOpen, ...props }: Props) {
  const widths = usePaneWidths(panelShown, panelWide, sideCollapsed)
  return <div {...props} style={{ ...style, '--zm-side-w': `${widths.side.value}px`, '--zm-panel-w': `${widths.panel.value}px` } as CSSProperties}>
    {children}
    {!sideCollapsed && !settingsOpen ? <PaneResizeHandle pane="side" label="调整对话列表宽度" {...widths.side} /> : null}
    {panelShown && !panelWide ? <PaneResizeHandle pane="panel" label="调整右栏宽度" {...widths.panel} /> : null}
  </div>
}

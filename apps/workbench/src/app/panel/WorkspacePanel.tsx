import { useId, type ComponentProps, type KeyboardEvent } from 'react'
import { ArrowLeft, FileDiff, Files, Globe, Maximize2, Minimize2, SquareTerminal, X } from 'lucide-react'
import { A } from '../text'
import { IconBtn } from '../ui'
import { ChangesPanel } from './Changes'
import { CommandsPanel } from './Commands'
import { FilesPanel } from './Files'
import { SessionChangesPanel } from './SessionChangesPanel'
import { ChangeHistory } from './ChangeHistory'
import { BrowserPanel } from './Browser'

export type PanelTab = 'files' | 'changes' | 'commands' | 'browser'
const icons = { files: Files, changes: FileDiff, commands: SquareTerminal, browser: Globe }

interface WorkspacePanelProps {
  tab: PanelTab
  onTab: (tab: PanelTab) => void
  title: string
  wide: boolean
  onToggleWide: () => void
  onClose: () => void
  onReturn: () => void
  files: ComponentProps<typeof FilesPanel> | null
  projectChanges: ComponentProps<typeof ChangesPanel> | null
  sessionChanges: ComponentProps<typeof SessionChangesPanel>
  commands: ComponentProps<typeof CommandsPanel>
}

// Durable saves and undo stay with Root; this component owns panel navigation and presentation.
export function WorkspacePanel(props: WorkspacePanelProps) {
  const id = useId()
  const tabs: PanelTab[] = props.files ? ['files', 'changes', 'commands', 'browser'] : ['changes', 'commands', 'browser']
  const navigate = (event: KeyboardEvent<HTMLButtonElement>, tab: PanelTab) => {
    const index = tabs.indexOf(tab)
    const next = event.key === 'ArrowRight' ? (index + 1) % tabs.length
      : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
        : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null
    if (next === null) return
    event.preventDefault()
    props.onTab(tabs[next])
    event.currentTarget.parentElement?.querySelectorAll<HTMLButtonElement>('[role="tab"]')[next]?.focus()
  }

  return <aside className="zm-side-panel" aria-label={A.panel.label}>
    <header className="zm-panel-head">
      <div className="zm-panel-context">
        <IconBtn size="sm" label="返回对话" icon={<ArrowLeft />} onClick={props.onReturn} />
        <span title={props.title}>{props.title}</span>
        <IconBtn size="sm" label={props.wide ? A.panel.shrinkHint : A.panel.widenHint} icon={props.wide ? <Minimize2 /> : <Maximize2 />} active={props.wide} onClick={props.onToggleWide} />
        <IconBtn size="sm" label={A.panel.close} icon={<X />} onClick={props.onClose} />
      </div>
      <div className="zm-panel-tabs" role="tablist" aria-label={A.panel.label}>
        {tabs.map((tab) => {
          const Icon = icons[tab]
          return <button key={tab} id={`${id}-${tab}`} type="button" role="tab" aria-selected={props.tab === tab}
            aria-controls={`${id}-body`} tabIndex={props.tab === tab ? 0 : -1}
            className={`zm-panel-tab${props.tab === tab ? ' is-active' : ''}`}
            onClick={() => props.onTab(tab)} onKeyDown={(event) => navigate(event, tab)}>
            <Icon aria-hidden /><span>{A.panel[tab]}</span>
          </button>
        })}
      </div>
    </header>
    <div id={`${id}-body`} className="zm-panel-body" role="tabpanel" aria-labelledby={`${id}-${props.tab}`}>
      {props.tab === 'browser' ? <BrowserPanel sessionId={props.commands.sessionId} /> : props.tab === 'commands' ? <CommandsPanel {...props.commands} />
        : props.tab === 'files' && props.files ? <FilesPanel {...props.files} />
          : <ChangeHistory key={`${props.sessionChanges.sessionId}:${props.projectChanges?.snapshot.project.project_id ?? ''}`} project={props.projectChanges} session={props.sessionChanges} />}
    </div>
  </aside>
}

import { useCallback, useEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowUpRight, Search, X } from 'lucide-react'
import type { ProjectInspection } from '../../api/projects'
import type { RuntimeSnapshot } from '../../api/runtime'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'
import type { ThemeChoice } from '../lib/theme'
import { Btn, IconBtn, type AskConfirm, type Notify } from '../ui'
import { MemoryTab } from './MemorySection'
import { ModelServicesTab } from './ModelsSection'
import { McpTab } from './McpSection'
import { SkillsTab } from './SkillsSection'
import { Appearance, Behavior, About } from './PreferenceSections'
import { ProjectInstructions } from './ProjectInstructions'
import { S } from './settingsText'
import { SETTINGS_PAGES, searchSettings, settingsPage, type SettingsPage, type SettingsSection } from './registry'
import { SettingsDraftContext } from './drafts'
import { SettingsTargetContext } from './useSettingsPane'
import { DraftRecoveryContext, recoveredSettingsSections } from './draftRecovery'
import './settings.css'

export type { SettingsSection } from './registry'
export { ProjectInstructions } from './ProjectInstructions'

/** Owns navigation and unsaved work; each section owns its persisted settings. */
export function SettingsView(props: {
  section: SettingsSection
  onSection: (section: SettingsSection) => void
  onClose: () => void
  runtime: RuntimeSnapshot
  theme: ThemeChoice
  onTheme: (theme: ThemeChoice) => boolean | void
  provider: ProviderSettingsApi
  onProviderChanged: () => Promise<void>
  /** 项目说明只作用于当前打开的文件夹。 */
  projectSnapshot: ProjectInspection | null
  onOpenProjectFile: (relativePath: string) => void
  onProjectSaved?: () => void
  notify: Notify
  askConfirm: AskConfirm
}) {
  const [query, setQuery] = useState('')
  const section = settingsPage(props.section)
  const [visited, setVisited] = useState<SettingsPage[]>(() => [...new Set([section, ...recoveredSettingsSections()])])
  const [drafts, setDrafts] = useState<Record<string, boolean>>({})
  const draftLabels = useRef<Record<string, string>>({})
  const [target, setTarget] = useState<string | null>(null)
  const root = useRef<HTMLElement>(null)
  const search = useRef<HTMLInputElement>(null)
  const recoveryDiscards = useRef(new Map<string, () => boolean>())
  const registerRecovery = useCallback((key: string, discard: () => boolean) => {
    recoveryDiscards.current.set(key, discard)
    return () => { if (recoveryDiscards.current.get(key) === discard) recoveryDiscards.current.delete(key) }
  }, [])
  const updateDraft = useCallback((section: string, dirty: boolean, description?: string) => {
    draftLabels.current[section] = description || SETTINGS_PAGES.find((page) => page.key === section)?.label || section
    setDrafts((before) => before[section] === dirty ? before : { ...before, [section]: dirty })
  }, [])
  const dirty = Object.values(drafts).some(Boolean)
  const current = SETTINGS_PAGES.find((item) => item.key === section)!
  const searching = Boolean(query.trim())
  const results = searchSettings(query)
  const askConfirm = props.askConfirm
  const notify = props.notify
  const leave = useCallback((action: () => void) => {
    if (!dirty) { action(); return }
    const names = Object.keys(drafts).filter((key) => drafts[key]).map((key) => draftLabels.current[key]).join('、')
    askConfirm({ title: '还有未保存的修改', body: `未保存：${names}。返回可继续编辑；离开将放弃这些草稿。`, confirmText: '放弃修改并离开', onConfirm: () => {
      const cleared = [...recoveryDiscards.current.values()].map(discard => discard())
      if (cleared.every(Boolean)) action()
      else notify('danger', '草稿备份未能清除，修改仍保留，请重试。')
    } })
  }, [dirty, drafts, askConfirm, notify])
  useEffect(() => { setVisited((before) => before.includes(section) ? before : [...before, section]) }, [section])
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null
    search.current?.focus()
    return () => previous?.focus()
  }, [])
  useEffect(() => {
    if (!dirty) return
    const onUnload = (event: BeforeUnloadEvent) => { event.preventDefault(); event.returnValue = '' }
    window.addEventListener('beforeunload', onUnload)
    return () => window.removeEventListener('beforeunload', onUnload)
  }, [dirty])
  useEffect(() => {
    if (!target || searching) return
    const focus = () => {
      const element = root.current?.querySelector<HTMLElement>(`[data-setting="${target}"]`)
      if (!element || element.closest('[hidden]')) return
      for (let ancestor: HTMLElement | null = element; ancestor; ancestor = ancestor.parentElement) {
        if (ancestor instanceof HTMLDetailsElement) ancestor.open = true
      }
      element.scrollIntoView?.({ block: 'center' })
      element.focus({ preventScroll: true })
      setTarget(null)
    }
    focus()
    const observer = new MutationObserver(focus)
    if (root.current) observer.observe(root.current, { childList: true, subtree: true, attributes: true, attributeFilter: ['hidden', 'open'] })
    return () => observer.disconnect()
  }, [target, searching, props.section, visited])
  return (
    <SettingsDraftContext.Provider value={updateDraft}>
    <DraftRecoveryContext.Provider value={registerRecovery}>
    <SettingsTargetContext.Provider value={target}>
    <section ref={root} className="zm-settings" aria-label={S.title} onKeyDown={(event) => {
      if (event.key !== 'Escape' || event.defaultPrevented || document.querySelector('[role="alertdialog"], [role="menu"]')) return
      if (searching) { setQuery(''); return }
      if ((event.target as HTMLElement).matches('textarea, input:not([aria-label="搜索设置"])')) return
      event.preventDefault()
      leave(props.onClose)
    }}>
      <nav className="zm-settings-nav" aria-label={S.title}>
        <Btn variant="ghost" icon={<ArrowLeft />} onClick={() => leave(props.onClose)}>返回工作台</Btn>
        <div className="zm-settings-brand"><h2>{S.title}</h2></div>
          <label className="zm-settings-search"><Search aria-hidden /><input ref={search} className="zm-input" value={query} onChange={(event) => setQuery(event.target.value)} placeholder={S.search} aria-label={S.search} />{query ? <IconBtn size="sm" label="清除搜索" icon={<X />} onClick={() => { setQuery(''); search.current?.focus() }} /> : null}</label>
        {Array.from(new Set(SETTINGS_PAGES.map((page) => page.group))).map((group) => (
          <div key={group} className="zm-settings-group">
            <p className="zm-settings-group-label">{group}</p>
            {SETTINGS_PAGES.filter((page) => page.group === group).map((item) => {
              const Icon = item.icon
              return (
                <button key={item.key} type="button" className={`zm-settings-link${item.key === section && !searching ? ' is-active' : ''}`} aria-current={item.key === section && !searching ? 'page' : undefined} onClick={() => { setQuery(''); props.onSection(item.key) }}>
                  <Icon aria-hidden />
                  <span>{item.label}</span>
                  {drafts[item.key] ? <span className="zm-settings-dirty" aria-label="未保存" /> : null}
                </button>
              )
            })}
          </div>
        ))}
        <div className="zm-settings-nav-foot"><span className={`zm-settings-health is-${props.runtime.connection}`} /><span>{props.runtime.connection === 'online' ? '本机服务已连接' : props.runtime.connection === 'offline' ? '本机服务离线' : '正在检查本机服务'}</span></div>
      </nav>
      <div className={`zm-settings-main${section === 'models' || section === 'mcp' ? ' is-connections' : ''}`}>
        <header className="zm-settings-head">
          <div><h1>{searching ? '搜索结果' : current.label}</h1><p>{searching ? `找到 ${results.length} 项相关设置` : current.description}</p>{!searching ? <span className="zm-settings-scope">{current.scope}</span> : null}</div>
          <IconBtn label={S.close} icon={<X />} onClick={() => leave(props.onClose)} />
        </header>
        {searching ? <div className="zm-settings-body zm-settings-results">
          {results.length === 0 ? <p className="zm-faint">{S.noResults}</p> : results.map((item) => <button key={item.id} type="button" onClick={() => { setTarget(item.id); setQuery(''); props.onSection(item.section) }}>
            <span><small>{SETTINGS_PAGES.find((page) => page.key === item.section)!.label}</small><strong>{item.label}</strong></span><ArrowUpRight aria-hidden />
          </button>)}
        </div> : null}
        {SETTINGS_PAGES.filter((page) => visited.includes(page.key) || page.key === section).map((page) => <div key={page.key} hidden={searching || section !== page.key} className="zm-settings-body" aria-label={`${page.label}内容`}>
          {page.key === 'models' ? (
            <ModelServicesTab runtime={props.runtime} provider={props.provider} onProviderChanged={props.onProviderChanged} notify={props.notify} askConfirm={props.askConfirm} />
          ) : null}
          {page.key === 'general' ? <><Appearance theme={props.theme} onTheme={props.onTheme} /><Behavior /></> : null}
          {page.key === 'memory' ? <MemoryTab notify={props.notify} /> : null}
          {page.key === 'skills' ? <SkillsTab notify={props.notify} askConfirm={props.askConfirm} /> : null}
          {page.key === 'mcp' ? <McpTab notify={props.notify} active={!searching && props.section === 'mcp'} /> : null}
          {page.key === 'project' ? <ProjectInstructions key={props.projectSnapshot?.project.project_id ?? 'none'} snapshot={props.projectSnapshot} onSaved={props.onProjectSaved} onOpen={(path) => leave(() => props.onOpenProjectFile(path))} /> : null}
          {page.key === 'about' ? <About runtime={props.runtime} notify={props.notify} /> : null}
        </div>)}
      </div>
    </section>
    </SettingsTargetContext.Provider>
    </DraftRecoveryContext.Provider>
    </SettingsDraftContext.Provider>
  )
}

import { SettingsDisclosure } from './navigation'
import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ClipboardPaste, Plus, RefreshCw, Trash2, X } from 'lucide-react'
import {
  createMcpServer, deleteMcpServer, listMcpServers, listMcpTools, reconnectMcpServer,
  setMcpServerEnabled, updateMcpServer, type McpServerView, type McpToolView,
} from '../../api/mcp'
import { authorFacingError } from '../../lib/authorFacingError'
import { RuntimeApiError } from '../../api/runtime'
import { subscribeSettingsChanges } from '../../lib/settingsChanges'
import { Btn, IconBtn, Switch, type Notify } from '../ui'
import { Field, Input, Notice, Textarea } from './kit'
import { SettingsConflict } from './SettingsConflict'
import { useSettingsDraft } from './drafts'
import { parseMcpImport } from './mcpImport'
import { useDraftRecovery } from './draftRecovery'
import { DraftRecoveryNotice } from './DraftRecoveryNotice'
import { parseMcpDraftSnapshot, type McpDraftSnapshot } from './mcpDraftRecovery'
import { makeDraft, fingerprint, mergeDraft, differences, updateBody, afterSave, type Draft, type SecretRow } from './mcpForm'
export { parseMcpArgs } from './mcpForm'
import './mcp.css'

const NEW = 'new'
const states: Record<McpServerView['state'], string> = { disabled: '已停用', connecting: '连接中', ready: '已连接', error: '连接失败' }
export function McpTab({ notify, active = true }: { notify: Notify; active?: boolean }) {
  const recovery = useDraftRecovery('mcp', parseMcpDraftSnapshot)
  const [servers, setServers] = useState<McpServerView[]>([])
  const [loaded, setLoaded] = useState(false)
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [selected, setSelected] = useState<string | null>(recovery.initial?.selected ?? null)
  const [drafts, setDrafts] = useState<Record<string, Draft>>(recovery.initial?.drafts ?? {})
  const currentDrafts = useRef(drafts)
  currentDrafts.current = drafts
  const [busy, setBusy] = useState<string | null>(null)
  const [formError, setFormError] = useState('')
  const [savedMessage, setSavedMessage] = useState<{ id: string; text: string; pending: boolean } | null>(null)
  const [toolsOpen, setToolsOpen] = useState(false)
  const [importOpen, setImportOpen] = useState(recovery.initial?.importOpen ?? false)
  const [importText, setImportText] = useState(recovery.initial?.importText ?? '')
  const [importError, setImportError] = useState('')
  const alive = useRef(true)
  const operation = useRef(false)
  const reading = useRef<AbortController | null>(null)
  const revision = useRef(0)
  const selectedServer = servers.find((server) => server.connection_id === selected)
  const draft = selected ? drafts[selected] : undefined
  const dirty = Boolean(importText.trim()) || Object.values(drafts).some((item) => fingerprint(item) !== item.original)
  const newDraft = selected === NEW || Boolean(selected?.startsWith('import:'))
  const removedElsewhere = !!draft && !newDraft && loaded && !selectedServer
  const configurationChanged = Boolean(draft?.revision && selectedServer?.revision && draft.revision !== selectedServer.revision)
  useSettingsDraft('mcp', dirty || Boolean(recovery.error))
  const pendingDrafts = Object.fromEntries(Object.entries(drafts).filter(([, item]) => fingerprint(item) !== item.original))
  const recoverySnapshot = JSON.stringify(dirty ? { selected, drafts: pendingDrafts, importText, importOpen } satisfies McpDraftSnapshot : null)
  const persistDraft = recovery.persist
  useLayoutEffect(() => { persistDraft(JSON.parse(recoverySnapshot) as McpDraftSnapshot | null) }, [persistDraft, recoverySnapshot])

  const refresh = useCallback(async () => {
    if (reading.current || operation.current) return
    const controller = new AbortController()
    const started = revision.current
    reading.current = controller
    setLoading(true)
    try {
      const result = await listMcpServers({ signal: controller.signal })
      if (!alive.current || controller.signal.aborted || started !== revision.current) return
      setServers(result.servers)
      setSavedMessage(message => {
        if (!message) return null
        const server = result.servers.find(item => item.connection_id === message.id)
        return !server || (message.pending && server.configuration_status === 'applied') ? null : message
      })
      setLoaded(true)
      setLoadError('')
      setSelected(value => {
        const creationId = value && currentDrafts.current[value]?.creationId
        return (creationId && result.servers.find(server => server.connection_id === creationId)?.connection_id)
          || value || result.servers[0]?.connection_id || null
      })
      setDrafts((before) => {
        const next = { ...before }
        // A reload may follow a successful create whose response was lost.
        // Reconcile its stable id without repeating the save or reconnect.
        for (const [id, item] of Object.entries(next)) {
          const created = item.creationId && result.servers.find(server => server.connection_id === item.creationId)
          if (!created) continue
          delete next[id]
          next[created.connection_id] = { ...item, base: created, revision: 'missing', creationId: undefined }
        }
        for (const server of result.servers) {
          const current = next[server.connection_id]
          if (!current || fingerprint(current) === current.original) next[server.connection_id] = makeDraft(server)
        }
        return next
      })
    } catch (error) {
      if (alive.current && !controller.signal.aborted && started === revision.current) setLoadError(authorFacingError(error, '无法读取 MCP 服务'))
    } finally {
      if (reading.current === controller) {
        reading.current = null
        if (alive.current) setLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    alive.current = true
    return () => { alive.current = false; reading.current?.abort() }
  }, [])
  useEffect(() => {
    if (!active) return
    void refresh()
    const unsubscribe = subscribeSettingsChanges('mcp', () => { void refresh() })
    const timer = window.setInterval(() => void refresh(), 2000)
    return () => { unsubscribe(); window.clearInterval(timer); reading.current?.abort(); reading.current = null }
  }, [active, refresh])

  const choose = (id: string) => { setSelected(id); setFormError(''); setToolsOpen(false) }
  const edit = (patch: Partial<Draft>) => {
    if (!selected) return
    setDrafts((before) => ({ ...before, [selected]: { ...before[selected], ...patch } }))
    setFormError('')
    setSavedMessage(null)
  }
  const applyServer = (server: McpServerView) => setServers((before) => before.some((item) => item.connection_id === server.connection_id)
    ? before.map((item) => item.connection_id === server.connection_id ? server : item) : [...before, server])

  const run = async (key: string, work: () => Promise<void>) => {
    if (operation.current) return
    operation.current = true
    revision.current += 1
    reading.current?.abort()
    reading.current = null
    setLoading(false)
    setBusy(key)
    setFormError('')
    let conflict = false
    try { await work() } catch (error) {
      conflict = error instanceof RuntimeApiError && error.status === 409
      if (alive.current) setFormError(authorFacingError(error, '操作未完成，请重试'))
      if (key === 'save' && draft?.creationId && selected) {
        try {
          const result = await listMcpServers({ timeoutMs: 5000 })
          const found = result.servers.find(item => item.connection_id === draft.creationId)
          if (found && alive.current) {
            setServers(result.servers)
            setDrafts(before => {
              const next = { ...before, [found.connection_id]: { ...draft, revision: 'missing', base: found, creationId: undefined } }
              delete next[selected]
              return next
            })
            setSelected(found.connection_id)
            setFormError('已找到这份草稿对应的连接。请核对最新配置后继续，上次请求可能已经保存。')
          }
        } catch { /* Keep the stable creation id, so retry cannot create a duplicate. */ }
      }
    } finally {
      operation.current = false
      if (alive.current) setBusy(null)
    }
    if (alive.current && conflict) await refresh()
  }

  const save = () => {
    if (!draft || !selected || configurationChanged || removedElsewhere) return
    const id = selected
    void run('save', async () => {
      const body = updateBody(draft, selectedServer?.enabled ?? draft.enabled ?? true, selectedServer)
      const server = newDraft ? await createMcpServer(body) : await updateMcpServer(id, body)
      if (!alive.current) return
      applyServer(server)
      setDrafts((before) => { const next = { ...before, [server.connection_id]: afterSave(draft, server) }; if (newDraft) delete next[id]; return next })
      setSelected(server.connection_id)
      const pending = server.configuration_status === 'pending'
      setSavedMessage({ id: server.connection_id, pending, text: pending ? '配置已保存，连接尚未应用。重新连接后可继续使用。' : 'MCP 配置已保存' })
      notify(pending ? 'warn' : 'ok', pending ? 'MCP 配置已保存，连接尚未应用' : 'MCP 配置已保存')
    })
  }

  const reset = () => {
    if (!selected) return
    if (newDraft) {
      setDrafts((before) => { const next = { ...before }; delete next[selected]; return next })
      const next = Object.keys(drafts).find((id) => id !== selected && (id === NEW || id.startsWith('import:'))) ?? servers[0]?.connection_id
      choose(next ?? NEW)
      if (!next) setSelected(null)
    } else if (selectedServer) setDrafts((before) => ({ ...before, [selected]: makeDraft(selectedServer) }))
    else {
      setDrafts((before) => { const next = { ...before }; delete next[selected]; return next })
      setSelected(servers[0]?.connection_id ?? null)
    }
    setFormError('')
    setSavedMessage(null)
  }

  const loadImport = () => {
    try {
      const configs = parseMcpImport(importText)
      const pending = Object.fromEntries(configs.map((config) => {
        const id = `import:${crypto.randomUUID()}`
        const item: Draft = { ...makeDraft(), name: config.name, transport: config.transport ?? 'stdio',
          url: config.url ?? '', command: config.command, args: JSON.stringify(config.args, null, 2), cwd: config.cwd ?? '',
          env: Object.entries(config.env).map(([name, value]) => ({ key: crypto.randomUUID(), name, value })),
          headers: Object.entries(config.headers ?? {}).map(([name, value]) => ({ key: crypto.randomUUID(), name, value })),
          enabled: config.enabled, original: '',
        }
        return [id, item]
      }))
      setDrafts((before) => ({ ...before, ...pending }))
      choose(Object.keys(pending)[0])
      setImportText('')
      setImportError('')
      setImportOpen(false)
    } catch (error) { setImportError(authorFacingError(error, '无法解析 MCP 配置')) }
  }

  return <section className="zm-mcp" data-setting="mcp-servers" tabIndex={-1} aria-label="MCP 服务">
    <DraftRecoveryNotice recovery={recovery} />
    <div className="zm-mcp-heading"><p className="zm-intro">连接 MCP 服务，让 Agent 使用它提供的工具。</p><div className="zm-btn-row">
      <Btn icon={<RefreshCw />} busy={loading} disabled={!!busy} onClick={() => void refresh()}>刷新</Btn>
      <Btn icon={<ClipboardPaste />} disabled={!!busy} aria-expanded={importOpen} onClick={() => setImportOpen((value) => !value)}>粘贴配置</Btn>
      <Btn icon={<Plus />} disabled={!!busy} onClick={() => { setDrafts((before) => ({ ...before, [NEW]: before[NEW] ?? makeDraft() })); choose(NEW) }}>添加服务</Btn>
    </div></div>
    {importOpen ? <section className="zm-mcp-import" aria-label="导入 MCP 配置">
      <Field label="MCP 配置 JSON" hint="粘贴服务提供的 mcpServers 配置。载入后可编辑，保存时才会连接；已有服务不会被覆盖。">
        <Textarea rows={8} value={importText} spellCheck={false} onChange={(event) => { setImportText(event.target.value); setImportError('') }} placeholder={'{\n  "mcpServers": {\n    "资料检索": { "command": "npx", "args": ["-y", "服务包名"] }\n  }\n}'} />
      </Field>
      {importError ? <p className="zm-mcp-error" role="alert">{importError}</p> : null}
      <div className="zm-btn-row"><Btn variant="primary" disabled={!importText.trim() || !!busy} onClick={loadImport}>载入配置</Btn><Btn variant="ghost" onClick={() => setImportOpen(false)}>收起</Btn></div>
    </section> : null}
    {loadError ? <p role="alert" className="zm-mcp-error">{loadError} <Btn size="sm" variant="ghost" onClick={() => void refresh()}>重试</Btn></p> : null}
    {!loaded && !loadError ? <p className="zm-faint">正在读取服务…</p> : null}
    <div className="zm-mcp-workspace"><aside className="zm-mcp-directory"><h2>已配置服务 <small>{servers.length}</small></h2>
    {Object.entries(drafts).filter(([id]) => id === NEW || id.startsWith('import:')).map(([id, item]) => <button type="button" key={id} className={`zm-mcp-server${selected === id ? ' is-selected' : ''}`} disabled={!!busy} onClick={() => choose(id)}><span>{item.name || '新连接'}</span><small>待保存{item.enabled === false ? ' · 停用' : ''}</small></button>)}
    {loaded ? Object.entries(drafts).filter(([id, item]) => id !== NEW && !id.startsWith('import:') && !servers.some(server => server.connection_id === id) && fingerprint(item) !== item.original).map(([id, item]) => <button type="button" key={id} className={`zm-mcp-server${selected === id ? ' is-selected' : ''}`} disabled={!!busy} onClick={() => choose(id)}><span>{item.name}</span><small>连接已移除 · 草稿保留</small></button>) : null}
    {servers.length ? <ul className="zm-mcp-servers" aria-label="已配置服务">{servers.map((server) => <li key={server.connection_id}>
      <button type="button" disabled={!!busy} className={`zm-mcp-server${selected === server.connection_id ? ' is-selected' : ''}`} aria-pressed={selected === server.connection_id} onClick={() => choose(server.connection_id)}>
        <span className="zm-mcp-server-name">{server.name}<span className={`zm-mcp-state is-${server.state}`}>{states[server.state]}</span></span>
        <span className="zm-mcp-server-command" title={server.transport === 'http' ? server.url ?? '' : server.command}>{server.transport === 'http' ? server.url : server.command}</span>
        <span className="zm-faint">{server.transport === 'http' ? '网络地址' : '本地程序'}{server.state === 'ready' ? ` · ${server.tool_count} 个工具` : ''}</span>
      </button>
    </li>)}</ul> : loaded && selected !== NEW ? <div className="zm-mcp-empty"><p>还没有连接 MCP 服务</p><span className="zm-faint">按服务的安装说明，添加本地程序或网络地址。</span></div> : null}

    </aside>
    {draft && selected ? <div className="zm-mcp-editor">
      <div className="zm-mcp-editor-head"><h2>{newDraft ? '添加 MCP 服务' : selectedServer?.name ?? '服务配置'}</h2>
        {selectedServer ? <div className="zm-btn-row"><span className={`zm-mcp-state is-${selectedServer.state}`}>{states[selectedServer.state]}</span>
          <Switch label="启用此 MCP 服务" checked={selectedServer.enabled} disabled={!!busy || configurationChanged} onChange={() => void run('enabled', async () => {
            const value = await setMcpServerEnabled(selectedServer.connection_id, !selectedServer.enabled, {}, draft.revision)
            if (alive.current) {
              applyServer(value)
              setDrafts(before => ({ ...before, [value.connection_id]: { ...before[value.connection_id], revision: value.revision, base: value, enabled: value.enabled } }))
            }
          })} />
          <Btn size="sm" disabled={!!busy || configurationChanged || fingerprint(draft) !== draft.original || (selectedServer.configuration_status !== 'pending' && (!selectedServer.enabled || selectedServer.state === 'connecting'))} title={fingerprint(draft) !== draft.original ? '先保存修改，再使用新配置连接' : '使用已保存的配置连接'} busy={busy === 'reconnect'} onClick={() => void run('reconnect', async () => { const value = await reconnectMcpServer(selectedServer.connection_id, {}, draft.revision); if (alive.current) { applyServer(value); setSavedMessage(null) } })}>重新连接</Btn>
        </div> : null}
      </div>
      {selectedServer ? <div className="zm-settings-effect" aria-label="MCP 配置作用范围"><span>来源：本机保存的配置</span><span>{selectedServer.configuration_status === 'pending' ? '已保存的配置尚未应用，请重新连接。' : selectedServer.state === 'ready' ? `当前连接提供 ${selectedServer.tool_count} 个工具，可调用情况见下方工具详情。` : selectedServer.enabled ? '配置已应用，连接完成后 Agent 才能使用工具。' : '服务已停用，Agent 不会调用此服务。'}</span></div> : null}
      {configurationChanged || removedElsewhere ? <SettingsConflict rows={differences(draft, selectedServer)} deleted={removedElsewhere} busy={!!busy}
        onReload={reset} onKeep={() => { if (selectedServer) { setDrafts(before => ({ ...before, [selected]: mergeDraft(draft, selectedServer) })); setFormError('') } }}
        onCopy={() => { const id = `import:${crypto.randomUUID()}`; setDrafts(before => { const next = { ...before, [id]: { ...draft, revision: undefined, base: undefined, creationId: `mcp-${crypto.randomUUID().replaceAll('-', '')}`, original: '' } }; delete next[selected]; return next }); choose(id) }} /> : null}
      {selectedServer?.apply_error ? <Notice kind="warn" role="status">{selectedServer.apply_error}</Notice> : null}
      {selectedServer?.error ? <p className="zm-mcp-error" role="status">{selectedServer.error}</p> : null}
      <form onSubmit={(event) => { event.preventDefault(); save() }}>
        <fieldset disabled={!!busy} className="zm-mcp-fields">
          <Field label="服务名称"><Input value={draft.name} onChange={(event) => edit({ name: event.target.value })} placeholder="例如：资料检索" autoComplete="off" /></Field>
          <div className="zm-mcp-transport"><span className="zm-field-label">连接方式</span><div className="zm-segment" role="group" aria-label="连接方式">
            <button type="button" className={draft.transport === 'stdio' ? 'is-on' : ''} aria-pressed={draft.transport === 'stdio'} onClick={() => edit({ transport: 'stdio' })}>本地程序</button>
            <button type="button" className={draft.transport === 'http' ? 'is-on' : ''} aria-pressed={draft.transport === 'http'} onClick={() => edit({ transport: 'http' })}>网络地址</button>
          </div></div>
          {draft.transport === 'http' ? <>
            <Field label="服务地址" hint="填写服务提供的 MCP 地址。"><Input className="zm-mcp-code" value={draft.url} onChange={(event) => edit({ url: event.target.value })} placeholder="https://example.com/mcp" autoComplete="off" spellCheck={false} /></Field>
            <SecretFields kind="headers" savedNames={selectedServer?.header_names ?? []} rows={draft.headers} removed={draft.removedHeaders} onRows={(headers) => edit({ headers })} onRemoved={(removedHeaders) => edit({ removedHeaders })} />
          </> : <>
          <Field label="启动程序" hint="填写程序名称或完整路径。参数填在下面，路径含空格也无需加引号。"><Input className="zm-mcp-code" value={draft.command} onChange={(event) => edit({ command: event.target.value })} placeholder="例如：npx、uvx 或程序的完整路径" autoComplete="off" spellCheck={false} /></Field>
          <Field label="启动参数（可选）" hint="每行一个参数，或粘贴 JSON 字符串数组；含空格的路径也放在同一行。"><Textarea className="zm-mcp-code" value={draft.args} onChange={(event) => edit({ args: event.target.value })} rows={4} placeholder={'-y\n@scope/mcp-server'} spellCheck={false} /></Field>
          <Field label="工作目录（可选）"><Input value={draft.cwd} onChange={(event) => edit({ cwd: event.target.value })} placeholder="留空使用用户主目录" autoComplete="off" spellCheck={false} /></Field>
          <SecretFields kind="env" savedNames={selectedServer?.env_names ?? []} rows={draft.env} removed={draft.removed} onRows={(env) => edit({ env })} onRemoved={(removed) => edit({ removed })} />
          </>}
        </fieldset>
        {formError ? <p className="zm-mcp-error" role="alert">{formError}</p> : null}
        {savedMessage?.id === selected ? <Notice kind={savedMessage.pending ? 'warn' : 'ok'} role="status">{savedMessage.text}</Notice> : null}
        <div className="zm-mcp-save-row zm-set-save"><Btn variant="primary" type="submit" busy={busy === 'save'} disabled={!!busy || configurationChanged || removedElsewhere}>{newDraft && draft.enabled !== false ? '保存并启用' : '保存配置'}</Btn><Btn variant="ghost" disabled={!!busy} onClick={reset}>{newDraft ? '取消' : '还原修改'}</Btn>
          {fingerprint(draft) !== draft.original ? <span className="zm-faint">未保存</span> : null}<span className="zm-spacer" />
          {selectedServer ? <Btn variant="ghost" icon={<Trash2 />} disabled={!!busy || configurationChanged} busy={busy === 'delete'} onClick={() => void run('delete', async () => {
            await deleteMcpServer(selectedServer.connection_id, {}, draft.revision)
            if (!alive.current) return
            setServers((before) => before.filter((item) => item.connection_id !== selectedServer.connection_id))
            setDrafts((before) => { const next = { ...before }; delete next[selectedServer.connection_id]; return next })
            setSelected(servers.find((item) => item.connection_id !== selectedServer.connection_id)?.connection_id ?? null)
            setToolsOpen(false)
            notify('ok', 'MCP 服务已移除')
          })}>移除服务</Btn> : null}
        </div>
      </form>
      <SettingsDisclosure id="mcp-tools" title={`查看工具（${selectedServer?.tool_count ?? 0}）`} open={toolsOpen} onToggle={() => setToolsOpen(value => !value)}>
        {selectedServer ? <McpTools key={selectedServer.connection_id} server={selectedServer} active={active && toolsOpen} /> : <p className="zm-faint">保存并连接服务后，可查看它提供的工具。</p>}
      </SettingsDisclosure>
    </div> : <div className="zm-settings-empty"><h2>把需要的工具接进来</h2><p>选择一项服务查看配置，或添加新的连接。</p></div>}
    </div>
  </section>
}

function SecretFields({ kind, savedNames, rows, removed, onRows, onRemoved }: { kind: 'env' | 'headers'; savedNames: string[]; rows: SecretRow[]; removed: string[]; onRows: (rows: SecretRow[]) => void; onRemoved: (names: string[]) => void }) {
  const label = kind === 'env' ? '变量' : '请求头'
  return <div className="zm-mcp-env"><div className="zm-mcp-env-head"><h3>{kind === 'env' ? '环境变量' : '请求头（可选）'}</h3><Btn size="sm" variant="ghost" icon={<Plus />} onClick={() => onRows([...rows, { key: crypto.randomUUID(), name: '', value: '' }])}>添加{label}</Btn></div>
    {savedNames.length ? <div className="zm-mcp-saved-env">{savedNames.map((name) => <span key={name} className={removed.includes(name) ? 'is-removed' : ''}><code>{name}</code><span>{removed.includes(name) ? '待移除' : '已保存'}</span><Btn size="sm" variant="ghost" aria-label={`${removed.includes(name) ? '撤销移除' : '移除'} ${name}`} onClick={() => onRemoved(removed.includes(name) ? removed.filter((item) => item !== name) : [...removed, name])}>{removed.includes(name) ? '撤销' : '移除'}</Btn></span>)}</div> : null}
    {rows.map((row, index) => <div key={row.key} className="zm-mcp-env-row"><Input aria-label={`${label} ${index + 1} 名称`} placeholder={kind === 'headers' ? '例如：Authorization' : '名称'} value={row.name} autoComplete="off" spellCheck={false} onChange={(event) => onRows(rows.map((item) => item.key === row.key ? { ...item, name: event.target.value } : item))} /><Input aria-label={`${label} ${index + 1} 值`} placeholder={kind === 'headers' ? '例如：Bearer …' : '值'} type="password" value={row.value} autoComplete="new-password" onChange={(event) => onRows(rows.map((item) => item.key === row.key ? { ...item, value: event.target.value } : item))} /><IconBtn label={`删除${label} ${index + 1}`} icon={<X />} onClick={() => onRows(rows.filter((item) => item.key !== row.key))} /></div>)}
    <p className="zm-faint">值仅在保存时写入；添加同名{label}可更新，值留空则保留原值。</p>
  </div>
}

function McpTools({ server, active }: { server: McpServerView; active: boolean }) {
  const [tools, setTools] = useState<McpToolView[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')
  const [refreshKey, setRefreshKey] = useState(0)
  useEffect(() => {
    if (!active || server.state !== 'ready') return
    const controller = new AbortController()
    setLoading(true)
    setError('')
    void listMcpTools(server.connection_id, { signal: controller.signal }).then((result) => { if (!controller.signal.aborted) setTools(result.tools) }, (reason) => { if (!controller.signal.aborted) setError(authorFacingError(reason, '工具列表读取失败')) }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [active, server.connection_id, server.state, server.tool_count, refreshKey])
  if (server.state !== 'ready') return <p className="zm-faint">{server.state === 'connecting' ? '连接完成后显示工具。' : '连接服务后可查看工具。'}</p>
  return <div className="zm-mcp-tools">
    <div className="zm-mcp-tools-top"><span className="zm-faint">{server.protocol_version ? `协议 ${server.protocol_version}` : '服务提供的工具'}</span><Btn size="sm" variant="ghost" busy={loading} onClick={() => setRefreshKey((value) => value + 1)}>刷新工具</Btn></div>
    {error ? <p role="alert" className="zm-mcp-error">{error}</p> : null}
    {!loading && !error && tools.length === 0 ? <p className="zm-faint">这个服务目前没有提供工具。</p> : null}
    {tools.map((tool) => <details key={tool.alias} className="zm-mcp-tool"><summary><strong>{tool.title || tool.raw_name}</strong><code>{tool.raw_name}</code>{tool.read_only ? <span className="zm-faint">服务声明：只读</span> : null}</summary>
      {tool.schema_error ? <p className="zm-mcp-error">暂不可调用：{tool.schema_error}</p> : null}
      {tool.description ? <p>{tool.description}</p> : null}<div className="zm-faint">Agent 调用名：<code>{tool.alias}</code></div><h4>输入参数</h4><pre>{JSON.stringify(tool.input_schema, null, 2)}</pre>
      {tool.annotations && Object.keys(tool.annotations).length ? <details className="zm-mcp-annotations"><summary>服务提供的声明</summary><pre>{JSON.stringify(tool.annotations, null, 2)}</pre></details> : null}
    </details>)}
  </div>
}

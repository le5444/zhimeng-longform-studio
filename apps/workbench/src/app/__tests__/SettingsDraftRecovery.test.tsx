import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { ProviderProfileView } from '../../api/providerSettings'
import type { McpServerView } from '../../api/mcp'
import { ModelServicesTab } from '../settings/ModelsSection'
import { McpTab } from '../settings/McpSection'
import { SettingsView } from '../settings/Settings'
import { recoveryKey } from '../settings/draftRecovery'
import { T } from '../strings'

const providers = vi.hoisted(() => ({ readProviderCatalog: vi.fn(), saveProviderProfile: vi.fn(), activateProviderProfile: vi.fn(),
  deleteProviderProfile: vi.fn(), fetchProviderModels: vi.fn(), revealProviderKey: vi.fn(), testProviderModel: vi.fn(),
  testProviderModelsLiveness: vi.fn(), clearVerifiedRoutes: vi.fn() }))
const mcp = vi.hoisted(() => ({ listMcpServers: vi.fn(), createMcpServer: vi.fn(), updateMcpServer: vi.fn(), listMcpTools: vi.fn(),
  reconnectMcpServer: vi.fn(), deleteMcpServer: vi.fn(), setMcpServerEnabled: vi.fn() }))
vi.mock('../../api/providerSettings', () => providers)
vi.mock('../../api/mcp', () => mcp)
const rev = (char: string) => char.repeat(64)
const profile = (extra: Partial<ProviderProfileView> = {}): ProviderProfileView => ({ provider_id: 'main', revision: rev('a'), name: '主服务',
  base_url: 'https://initial.invalid/v1', model: 'initial', selected_models: ['initial'], wire_format: 'chat', reasoning_effort: 'auto',
  api_key_set: true, configured: true, active: true, context_window: '256k', accepted_reported_models: [], ...extra })
const catalog = (items: ProviderProfileView[]) => ({ schema: 'zhimeng.provider-catalog.v1', providers: items, active_provider_id: 'main', revision: rev('f') })
const server = (extra: Partial<McpServerView> = {}): McpServerView => ({ connection_id: 'local', name: '文件服务', revision: rev('a'),
  command: 'node', args: ['original.js'], cwd: null, enabled: false, env_names: ['TOKEN'], state: 'disabled', error: null, tool_count: 0, protocol_version: null, ...extra })
const props = () => ({ runtime: { connection: 'online' as const, ready: true, serviceStatus: 'ready', runStatus: null, version: 'test', detail: '', checkedAt: '' },
  provider: { settings: null, catalog: null, state: 'ready' as const, reload: vi.fn() }, notify: vi.fn(), askConfirm: vi.fn(), onProviderChanged: vi.fn(async () => {}) })
const fill = (label: string | RegExp, value: string) => fireEvent.change(screen.getByLabelText(label), { target: { value } })
const model = () => render(<ModelServicesTab {...props()} />)
const tools = () => render(<McpTab notify={vi.fn()} />)

beforeEach(() => {
  sessionStorage.clear()
  vi.resetAllMocks()
  providers.readProviderCatalog.mockResolvedValue(catalog([profile()]))
  mcp.listMcpServers.mockResolvedValue({ servers: [server()] })
  mcp.listMcpTools.mockResolvedValue({ tools: [] })
})
afterEach(() => { cleanup(); vi.restoreAllMocks() })

it('restores model drafts for multiple resources, secrets and original versions without saving or testing', async () => {
  const backup = profile({ provider_id: 'backup', name: '备用', active: false })
  providers.readProviderCatalog.mockResolvedValue(catalog([profile(), backup]))
  const first = model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.baseUrl, 'https://mine.invalid/v1')
  fill(T.settings.apiKey, 'typed-secret')
  fireEvent.click(screen.getByRole('button', { name: /^备用/ }))
  fill(T.settings.apiKey, 'backup-secret')
  first.unmount()
  const latest = profile({ revision: rev('b'), model: 'new-model', selected_models: ['new-model'] })
  providers.readProviderCatalog.mockResolvedValue(catalog([latest, backup]))
  model()
  await screen.findByText(/已恢复未保存的草稿/)
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('backup-secret')
  await waitFor(() => expect(providers.readProviderCatalog).toHaveBeenCalledTimes(2))
  fireEvent.click(screen.getByRole('button', { name: /^主服务/ }))
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('typed-secret')
  expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://mine.invalid/v1')
  expect(screen.getByRole('button', { name: T.settings.saveService })).toBeDisabled()
  expect(providers.saveProviderProfile).not.toHaveBeenCalled()
  expect(providers.testProviderModel).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  const saved = { ...latest, base_url: 'https://mine.invalid/v1', revision: rev('c') }
  providers.saveProviderProfile.mockResolvedValue({ profile: saved, catalog: catalog([saved, backup]) })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await waitFor(() => expect(providers.saveProviderProfile).toHaveBeenCalledWith(expect.objectContaining({ expectedRevision: rev('b'), apiKey: 'typed-secret', model: 'new-model' })))
  await waitFor(() => expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue(''))
  expect(sessionStorage.getItem(recoveryKey('models'))).not.toContain('typed-secret')
  expect(sessionStorage.getItem(recoveryKey('models'))).toContain('backup-secret')
})

it('reuses the new model identity after reload and reconciles an already committed create', async () => {
  const first = model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fireEvent.click(screen.getByRole('button', { name: T.settings.addService }))
  fill(T.settings.baseUrl, 'https://new.invalid/v1')
  fill(/^模型选择/, 'new-model')
  const savedDraft = JSON.parse(sessionStorage.getItem(recoveryKey('models'))!)
  first.unmount()
  const created = profile({ provider_id: savedDraft.value.creationId, name: '上次已创建', base_url: 'https://new.invalid/v1', revision: rev('d') })
  providers.readProviderCatalog.mockResolvedValue(catalog([profile(), created]))
  model()
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://new.invalid/v1')
  expect(providers.saveProviderProfile).not.toHaveBeenCalled()
  const recovered = JSON.parse(sessionStorage.getItem(recoveryKey('models'))!).value
  expect(recovered.selection).toEqual({ kind: 'edit', id: created.provider_id })
  expect(recovered.drafts[0][1].profile.revision).toBe('missing')
})

it('saves a restored new model with its original creation identity', async () => {
  const first = model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fireEvent.click(screen.getByRole('button', { name: T.settings.addService }))
  fill(T.settings.baseUrl, 'https://new.invalid/v1')
  fill(/^模型选择/, 'new-model')
  const id = JSON.parse(sessionStorage.getItem(recoveryKey('models'))!).value.creationId
  first.unmount()
  model()
  await waitFor(() => expect(providers.readProviderCatalog).toHaveBeenCalledTimes(2))
  const created = profile({ provider_id: id, base_url: 'https://new.invalid/v1', model: 'new-model' })
  providers.saveProviderProfile.mockResolvedValue({ profile: created, catalog: catalog([profile(), created]) })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await waitFor(() => expect(providers.saveProviderProfile).toHaveBeenCalledWith(expect.objectContaining({ providerId: id, expectedRevision: 'missing' })))
  await waitFor(() => expect(sessionStorage.getItem(recoveryKey('models'))).toBeNull())
})

it('does not copy a revealed saved credential into the draft backup', async () => {
  model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  providers.revealProviderKey.mockResolvedValue({ api_key: 'saved-revealed-secret' })
  fireEvent.click(screen.getByRole('button', { name: T.settings.showSavedKey }))
  await screen.findByText('saved-revealed-secret')
  fill(T.settings.baseUrl, 'https://mine.invalid/v1')
  expect(sessionStorage.getItem(recoveryKey('models'))).toContain('https://mine.invalid/v1')
  expect(sessionStorage.getItem(recoveryKey('models'))).not.toContain('saved-revealed-secret')
})

it('restores MCP variables, removal choices and version, and clears only the saved resource', async () => {
  const first = tools()
  await screen.findByLabelText('服务名称')
  fill(/^启动参数/, 'mine.js')
  fireEvent.click(screen.getByRole('button', { name: '移除 TOKEN' }))
  fireEvent.click(screen.getByRole('button', { name: '添加变量' }))
  fill('变量 1 名称', 'NEW_TOKEN'); fill('变量 1 值', 'new-secret')
  first.unmount()
  mcp.listMcpServers.mockResolvedValue({ servers: [server({ revision: rev('b'), cwd: 'C:/new-location' })] })
  tools()
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(screen.getByLabelText('变量 1 值')).toHaveValue('new-secret')
  expect(screen.getByText('待移除')).toBeInTheDocument()
  expect(mcp.updateMcpServer).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  mcp.updateMcpServer.mockResolvedValue(server({ revision: rev('c'), args: ['mine.js'], cwd: 'C:/new-location', env_names: ['NEW_TOKEN'] }))
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(mcp.updateMcpServer).toHaveBeenCalledWith('local', expect.objectContaining({ expected_revision: rev('b'),
    enabled: false, env: { NEW_TOKEN: 'new-secret' }, remove_env: ['TOKEN'], cwd: 'C:/new-location' })))
  await waitFor(() => expect(sessionStorage.getItem(recoveryKey('mcp'))).toBeNull())
})

it('restores imported MCP drafts without duplicate row ids or replaying connections', async () => {
  mcp.listMcpServers.mockResolvedValue({ servers: [] })
  const first = tools()
  await screen.findByText('还没有连接 MCP 服务')
  fireEvent.click(screen.getByRole('button', { name: '粘贴配置' }))
  fill(/^MCP 配置 JSON/, JSON.stringify({ mcpServers: { first: { command: 'node', args: ['one.js'], env: { TOKEN: 'one' } } } }))
  fireEvent.click(screen.getByRole('button', { name: '载入配置' }))
  const previous = JSON.parse(sessionStorage.getItem(recoveryKey('mcp'))!).value
  first.unmount()
  tools()
  await screen.findByText(/已恢复未保存的草稿/)
  expect(screen.getByLabelText('变量 1 值')).toHaveValue('one')
  fireEvent.click(screen.getByRole('button', { name: '添加变量' }))
  fill('变量 2 名称', 'SECOND'); fill('变量 2 值', 'two')
  const restored = JSON.parse(sessionStorage.getItem(recoveryKey('mcp'))!).value
  const draft = restored.drafts[restored.selected]
  expect(new Set(draft.env.map((row: { key: string }) => row.key)).size).toBe(2)
  expect(draft.creationId).toBe(previous.drafts[previous.selected].creationId)
  expect(mcp.createMcpServer).not.toHaveBeenCalled()
  expect(mcp.reconnectMcpServer).not.toHaveBeenCalled()
})

it('reconciles a disabled MCP create across remount without replay or stale completion', async () => {
  mcp.listMcpServers.mockResolvedValue({ servers: [] })
  const first = tools()
  await screen.findByText('还没有连接 MCP 服务')
  fireEvent.click(screen.getByRole('button', { name: '粘贴配置' }))
  fill(/^MCP 配置 JSON/, JSON.stringify({ mcpServers: { pending: { command: 'node', args: ['one.js'], disabled: true } } }))
  fireEvent.click(screen.getByRole('button', { name: '载入配置' }))
  const draft = JSON.parse(sessionStorage.getItem(recoveryKey('mcp'))!).value
  const id = draft.drafts[draft.selected].creationId
  let finish!: (value: McpServerView) => void
  mcp.createMcpServer.mockReturnValue(new Promise<McpServerView>(resolve => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  expect(mcp.createMcpServer).toHaveBeenCalledWith(expect.objectContaining({ creation_id: id, enabled: false }))
  first.unmount()
  const created = server({ connection_id: id, name: 'pending', args: ['one.js'] })
  mcp.listMcpServers.mockResolvedValue({ servers: [created] })
  tools()
  await screen.findByRole('status', { name: '配置变更比较' })
  fill('服务名称', 'keep editing')
  const before = sessionStorage.getItem(recoveryKey('mcp'))
  await act(async () => finish(created))
  expect(sessionStorage.getItem(recoveryKey('mcp'))).toBe(before)
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  mcp.updateMcpServer.mockResolvedValue({ ...created, name: 'keep editing', revision: rev('b') })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(mcp.updateMcpServer).toHaveBeenCalledWith(id, expect.objectContaining({ name: 'keep editing', enabled: false, expected_revision: rev('a') })))
  await waitFor(() => expect(sessionStorage.getItem(recoveryKey('mcp'))).toBeNull())
  expect(mcp.createMcpServer).toHaveBeenCalledOnce()
  expect(mcp.reconnectMcpServer).not.toHaveBeenCalled()
})

it('retains unfinished MCP import text without attempting to create anything', async () => {
  const first = tools()
  await screen.findByLabelText('服务名称')
  fireEvent.click(screen.getByRole('button', { name: '粘贴配置' }))
  fill(/^MCP 配置 JSON/, '{"mcpServers":')
  first.unmount()
  tools()
  await screen.findByText(/已恢复未保存的草稿/)
  expect(screen.getByLabelText(/^MCP 配置 JSON/)).toHaveValue('{"mcpServers":')
  expect(mcp.createMcpServer).not.toHaveBeenCalled()
})

it('keeps form input on storage failure and makes retry truthful', async () => {
  model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('quota') })
  fill(T.settings.apiKey, 'still-in-form')
  await screen.findByText(/草稿备份未能更新/)
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('still-in-form')
  expect(sessionStorage.getItem(recoveryKey('models'))).toBeNull()
  set.mockRestore()
  fireEvent.click(screen.getByRole('button', { name: '重试备份' }))
  await waitFor(() => expect(screen.queryByText(/草稿备份未能更新/)).toBeNull())
  expect(sessionStorage.getItem(recoveryKey('models'))).toContain('still-in-form')
})

it('preserves malformed backups until explicitly removed and keeps editing usable', async () => {
  sessionStorage.setItem(recoveryKey('models'), '{broken')
  model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.apiKey, 'current-input')
  expect(sessionStorage.getItem(recoveryKey('models'))).toBe('{broken')
  fireEvent.click(screen.getByRole('button', { name: '移除旧备份，保留当前输入' }))
  expect(sessionStorage.getItem(recoveryKey('models'))).toContain('current-input')
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('current-input')
})

it('keeps a structurally invalid restored profile from breaking the settings screen', async () => {
  const first = model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.apiKey, 'recoverable-input')
  const snapshot = JSON.parse(sessionStorage.getItem(recoveryKey('models'))!)
  first.unmount()
  snapshot.value.drafts[0][1].profile.model_overrides = { initial: { extra_header_names: 42 } }
  const raw = JSON.stringify(snapshot)
  sessionStorage.setItem(recoveryKey('models'), raw)
  model()
  await screen.findByText(/旧草稿格式无法读取/)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  expect(sessionStorage.getItem(recoveryKey('models'))).toBe(raw)
  expect(providers.saveProviderProfile).not.toHaveBeenCalled()
})

it('mounts recovered sections when opening another category and clears their backups on explicit discard', async () => {
  const first = model()
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.apiKey, 'hidden-draft')
  first.unmount()
  const settings = { ...props(), section: 'general' as const, onSection: vi.fn(), onClose: vi.fn(), theme: 'light' as const,
    onTheme: vi.fn(), projectSnapshot: null, onOpenProjectFile: vi.fn() }
  render(<SettingsView {...settings} />)
  await waitFor(() => expect(document.querySelector('[aria-label="模型服务内容"] input[type="password"]')).toHaveValue('hidden-draft'))
  fireEvent.click(screen.getByRole('button', { name: '返回工作台' }))
  expect(settings.askConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '还有未保存的修改' }))
  await act(async () => settings.askConfirm.mock.calls[0][0].onConfirm())
  expect(settings.onClose).toHaveBeenCalledOnce()
  expect(sessionStorage.getItem(recoveryKey('models'))).toBeNull()
})

it('stays in settings when clearing the backup fails, then allows explicit retry', async () => {
  const settings = { ...props(), section: 'models' as const, onSection: vi.fn(), onClose: vi.fn(), theme: 'light' as const,
    onTheme: vi.fn(), projectSnapshot: null, onOpenProjectFile: vi.fn() }
  render(<SettingsView {...settings} />)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.apiKey, 'keep-until-discarded')
  const remove = vi.spyOn(Storage.prototype, 'removeItem').mockImplementation(() => { throw new Error('unavailable') })
  fireEvent.click(screen.getByRole('button', { name: '返回工作台' }))
  await act(async () => settings.askConfirm.mock.calls[0][0].onConfirm())
  expect(settings.onClose).not.toHaveBeenCalled()
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('keep-until-discarded')
  expect(sessionStorage.getItem(recoveryKey('models'))).toContain('keep-until-discarded')
  remove.mockRestore()
  fireEvent.click(screen.getByRole('button', { name: '返回工作台' }))
  await act(async () => settings.askConfirm.mock.calls[1][0].onConfirm())
  expect(settings.onClose).toHaveBeenCalledOnce()
  expect(sessionStorage.getItem(recoveryKey('models'))).toBeNull()
})

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterAll, afterEach, beforeAll, beforeEach, expect, it, vi } from 'vitest'
import type { ProviderProfileView } from '../../api/providerSettings'
import type { McpServerView } from '../../api/mcp'
import { RuntimeApiError } from '../../api/runtime'
import { publishSettingsChange } from '../../lib/settingsChanges'
import { ModelServicesTab } from '../settings/ModelsSection'
import { McpTab } from '../settings/McpSection'
import { ModelConnectionEditor } from '../settings/ModelConnectionEditor'
import type { ModelServicesTabProps } from '../settings/useModelServices'
import { T } from '../strings'

const providerApi = vi.hoisted(() => ({ readProviderCatalog: vi.fn(), saveProviderProfile: vi.fn(), activateProviderProfile: vi.fn(),
  deleteProviderProfile: vi.fn(), fetchProviderModels: vi.fn(), testProviderModel: vi.fn(), testProviderModelsLiveness: vi.fn(), clearVerifiedRoutes: vi.fn(), revealProviderKey: vi.fn(), patchProviderModelOverride: vi.fn() }))
const mcpApi = vi.hoisted(() => ({ listMcpServers: vi.fn(), createMcpServer: vi.fn(), updateMcpServer: vi.fn(), deleteMcpServer: vi.fn(),
  setMcpServerEnabled: vi.fn(), reconnectMcpServer: vi.fn(), listMcpTools: vi.fn() }))
vi.mock('../../api/providerSettings', () => providerApi)
vi.mock('../../api/mcp', () => mcpApi)

const revision = (letter: string) => letter.repeat(64)
function profile(overrides: Partial<ProviderProfileView> = {}): ProviderProfileView {
  return { provider_id: 'main', name: '主服务', revision: revision('a'), source: 'settings', applies_to: 'next_request',
    base_url: 'https://initial.invalid/v1', model: 'initial', selected_models: ['initial'], reasoning_effort: 'auto', wire_format: 'chat',
    accepted_reported_models: [], api_key_set: true, context_window: '256k', configured: true, active: true, ...overrides }
}
function catalog(profiles: ProviderProfileView[]) {
  return { schema: 'zhimeng.provider-catalog.v1', revision: revision('f'), active_provider_id: profiles.find(item => item.active)?.provider_id ?? null, providers: profiles }
}
function props(): ModelServicesTabProps {
  return { runtime: { connection: 'online', ready: true, serviceStatus: 'ready', runStatus: null, version: 'test', detail: '', checkedAt: '' },
    provider: { settings: null, catalog: null, state: 'ready', reload: vi.fn() }, notify: vi.fn(), askConfirm: vi.fn(), onProviderChanged: vi.fn().mockResolvedValue(undefined) }
}
function server(overrides: Partial<McpServerView> = {}): McpServerView {
  return { connection_id: 'local', name: '资料工具', revision: revision('a'), applied_revision: revision('a'), configuration_status: 'applied',
    command: 'node', args: ['before.js'], cwd: 'C:\\sample', enabled: false, env_names: [], state: 'disabled', error: null, tool_count: 0, protocol_version: null, ...overrides }
}
const conflict = () => new RuntimeApiError(409, { error: { code: 'settings_conflict', message: '设置已在其他地方修改，草稿保留。' } })
function fill(label: string, value: string) { fireEvent.change(screen.getByLabelText(label), { target: { value } }) }

beforeEach(() => {
  sessionStorage.clear()
  vi.resetAllMocks()
  providerApi.readProviderCatalog.mockResolvedValue(catalog([profile()]))
  mcpApi.listMcpServers.mockResolvedValue({ servers: [server()] })
  mcpApi.listMcpTools.mockResolvedValue({ tools: [] })
})
afterEach(cleanup)
beforeAll(() => Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.open = true } }))
afterAll(() => { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal') })

it('配置损坏的实际原因显示在模型目录并可重试恢复', async () => {
  providerApi.readProviderCatalog.mockRejectedValue(new RuntimeApiError(503, { error: {
    code: 'provider_settings_unreadable', message: '模型配置中的字段格式不正确，原文件已保留；请修复后重试。',
  } }))
  render(<ModelServicesTab {...props()} />)
  await screen.findByText(/模型配置中的字段格式不正确/)
  expect(screen.queryByText('添加第一个模型服务')).toBeNull()
  providerApi.readProviderCatalog.mockResolvedValue(catalog([profile()]))
  fireEvent.click(screen.getByRole('button', { name: T.common.retry }))
  await screen.findByDisplayValue('https://initial.invalid/v1')
  expect(screen.queryByText(/模型配置中的字段格式不正确/)).toBeNull()
})

it('模型例外冲突合并只保留自己修改的字段，保留密钥并采用最新输出长度', async () => {
  const initial = profile({ model_overrides: { initial: { overridden_fields: ['base_url', 'max_output_tokens'],
    api_key_set: true, extra_header_names: [], base_url: 'https://override.invalid/v1', max_output_tokens: 1000 } } })
  const latest = profile({ revision: revision('b'), model_overrides: { initial: {
    ...initial.model_overrides!.initial, max_output_tokens: 2000,
  } } })
  const saved = vi.fn()
  render(<ModelConnectionEditor profile={initial} model="initial" onSaved={saved} onClose={vi.fn()} askConfirm={vi.fn()} />)
  fill('模型地址', 'https://mine.invalid/v1')
  fill('模型密钥', 'override-draft-secret')
  providerApi.patchProviderModelOverride.mockRejectedValueOnce(conflict())
  providerApi.readProviderCatalog.mockResolvedValue(catalog([latest]))
  fireEvent.click(screen.getByRole('button', { name: '保存例外设置' }))
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(providerApi.patchProviderModelOverride).toHaveBeenCalledWith('main', 'initial', expect.objectContaining({ expected_revision: revision('a') }))
  expect(screen.getByLabelText('模型密钥')).toHaveValue('override-draft-secret')
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  expect(screen.getByLabelText('模型地址')).toHaveValue('https://mine.invalid/v1')
  expect(screen.getByLabelText('输出长度')).toHaveValue(2000)
  expect(saved).not.toHaveBeenCalled()
  providerApi.patchProviderModelOverride.mockResolvedValue(catalog([latest]))
  fireEvent.click(screen.getByRole('button', { name: '保存例外设置' }))
  await waitFor(() => expect(saved).toHaveBeenCalledOnce())
  expect(providerApi.patchProviderModelOverride.mock.calls[1][2]).toMatchObject({ expected_revision: revision('b'),
    base_url: 'https://mine.invalid/v1', max_output_tokens: 2000, api_key: 'override-draft-secret' })
})

it('旧模型草稿发生冲突时保留密钥，比较后只合并自己的修改，再携带新版本保存', async () => {
  render(<ModelServicesTab {...props()} />)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.baseUrl, 'https://my-edit.invalid/v1')
  fill(T.settings.apiKey, 'draft-only-secret')
  const latest = profile({ revision: revision('b'), model: 'external-model', selected_models: ['external-model'] })
  providerApi.readProviderCatalog.mockResolvedValue(catalog([latest]))
  providerApi.saveProviderProfile.mockRejectedValueOnce(conflict())
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(providerApi.saveProviderProfile.mock.calls[0][0].expectedRevision).toBe(revision('a'))
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('draft-only-secret')
  expect(screen.getByRole('button', { name: T.settings.saveService })).toBeDisabled()
  expect(within(screen.getByRole('status', { name: '配置变更比较' })).queryByText('draft-only-secret')).toBeNull()
  fireEvent.click(screen.getByText('比较最新配置与我的草稿'))
  expect(screen.getByRole('table')).toBeVisible()
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://my-edit.invalid/v1')
  expect(screen.getByDisplayValue('external-model')).toBeInTheDocument()
  expect(providerApi.saveProviderProfile).toHaveBeenCalledTimes(1)
  const saved = { ...latest, revision: revision('c'), base_url: 'https://my-edit.invalid/v1' }
  providerApi.saveProviderProfile.mockResolvedValue({ catalog: catalog([saved]), profile: saved })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await waitFor(() => expect(providerApi.saveProviderProfile).toHaveBeenCalledTimes(2))
  expect(providerApi.saveProviderProfile.mock.calls[1][0]).toMatchObject({ expectedRevision: revision('b'), model: 'external-model', apiKey: 'draft-only-secret', makeActive: false })
  await waitFor(() => expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue(''))
})

it('后台更新自动刷新干净模型表单，脏草稿在资源切换后仍绑定原版本', async () => {
  const backup = profile({ provider_id: 'backup', name: '备用', active: false })
  providerApi.readProviderCatalog.mockResolvedValue(catalog([profile(), backup]))
  render(<ModelServicesTab {...props()} />)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  let latest = profile({ revision: revision('b'), base_url: 'https://external.invalid/v1' })
  providerApi.readProviderCatalog.mockResolvedValue(catalog([latest, backup]))
  await act(async () => publishSettingsChange('providers'))
  await screen.findByDisplayValue('https://external.invalid/v1')
  expect(screen.queryByRole('status', { name: '配置变更比较' })).toBeNull()
  fill(T.settings.apiKey, 'keep-across-resources')
  fireEvent.click(screen.getByRole('button', { name: /^备用/ }))
  latest = { ...latest, revision: revision('c'), base_url: 'https://second-external.invalid/v1' }
  providerApi.readProviderCatalog.mockResolvedValue(catalog([latest, backup]))
  await act(async () => publishSettingsChange('providers'))
  fireEvent.click(screen.getByRole('button', { name: /^主服务/ }))
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('keep-across-resources')
  expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://external.invalid/v1')
  expect(providerApi.saveProviderProfile).not.toHaveBeenCalled()
})

it('模型连接被外部删除后保留草稿并能转为新连接，不复活旧编号', async () => {
  render(<ModelServicesTab {...props()} />)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  fill(T.settings.baseUrl, 'https://my-edit.invalid/v1')
  providerApi.readProviderCatalog.mockResolvedValue(catalog([]))
  await act(async () => publishSettingsChange('providers'))
  await screen.findByText('这个连接已在其他地方移除')
  fireEvent.click(screen.getByRole('button', { name: '另存为新连接' }))
  expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://my-edit.invalid/v1')
  const saved = profile({ provider_id: 'new-id' })
  providerApi.saveProviderProfile.mockResolvedValue({ catalog: catalog([saved]), profile: saved })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await waitFor(() => expect(providerApi.saveProviderProfile).toHaveBeenCalledTimes(1))
  expect(providerApi.saveProviderProfile.mock.calls[0][0].providerId).toMatch(/^provider-[a-f0-9]{32}$/)
  expect(providerApi.saveProviderProfile.mock.calls[0][0].expectedRevision).toBe('missing')
})

it('后台更新配置后收起旧密钥，旧版本迟到的显示请求不能重新出现', async () => {
  render(<ModelServicesTab {...props()} />)
  await screen.findByDisplayValue('https://initial.invalid/v1')
  providerApi.revealProviderKey.mockResolvedValueOnce({ api_key: 'old-saved-key' })
  await act(async () => fireEvent.click(screen.getByRole('button', { name: T.settings.showSavedKey })))
  expect(await screen.findByTestId('revealed-key')).toHaveTextContent('old-saved-key')
  const latest = profile({ revision: revision('b') })
  providerApi.readProviderCatalog.mockResolvedValue(catalog([latest]))
  await act(async () => publishSettingsChange('providers'))
  expect(screen.queryByTestId('revealed-key')).toBeNull()
  let finish!: (value: { api_key: string }) => void
  providerApi.revealProviderKey.mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
  fireEvent.click(screen.getByRole('button', { name: T.settings.showSavedKey }))
  providerApi.readProviderCatalog.mockResolvedValue(catalog([profile({ revision: revision('c') })]))
  await act(async () => publishSettingsChange('providers'))
  await act(async () => finish({ api_key: 'stale-key-from-old-request' }))
  expect(screen.queryByTestId('revealed-key')).toBeNull()
})

it('MCP 后台变更不改写编辑版本，比较合并保留本地名称并采用最新启动参数', async () => {
  render(<McpTab notify={vi.fn()} />)
  await screen.findByLabelText('服务名称')
  fill('服务名称', '我的工具名')
  const latest = server({ revision: revision('b'), applied_revision: revision('a'), configuration_status: 'pending', args: ['external.js'] })
  mcpApi.listMcpServers.mockResolvedValue({ servers: [latest] })
  await act(async () => publishSettingsChange('mcp'))
  await screen.findByRole('status', { name: '配置变更比较' })
  expect(screen.getByLabelText('服务名称')).toHaveValue('我的工具名')
  expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled()
  fireEvent.click(screen.getByRole('button', { name: '合并我的修改，继续编辑' }))
  expect(screen.getByLabelText(/启动参数/)).toHaveValue('[\n  "external.js"\n]')
  mcpApi.updateMcpServer.mockResolvedValue(server({ name: '我的工具名', revision: revision('c'), applied_revision: revision('c') }))
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(mcpApi.updateMcpServer).toHaveBeenCalledWith('local', expect.objectContaining({ expected_revision: revision('b'), name: '我的工具名', args: ['external.js'] })))
})

it('MCP 已保存但未应用时保留资源编号和明确反馈，重新连接不会重复创建', async () => {
  const notify = vi.fn()
  render(<McpTab notify={notify} />)
  await screen.findByLabelText('服务名称')
  fill('服务名称', '已存新配置')
  const pending = server({ name: '已存新配置', revision: revision('b'), configuration_status: 'pending', apply_error: '合成运行时失败' })
  mcpApi.updateMcpServer.mockResolvedValue(pending)
  mcpApi.reconnectMcpServer.mockResolvedValue({ ...pending, configuration_status: 'applied', applied_revision: revision('b'), apply_error: null })
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await screen.findByText('配置已保存，连接尚未应用。重新连接后可继续使用。')
  expect(notify).toHaveBeenCalledWith('warn', 'MCP 配置已保存，连接尚未应用')
  fireEvent.click(screen.getByRole('button', { name: '重新连接' }))
  await waitFor(() => expect(mcpApi.reconnectMcpServer).toHaveBeenCalledWith('local', {}, revision('b')))
  expect(mcpApi.createMcpServer).not.toHaveBeenCalled()
  await waitFor(() => expect(screen.queryByText('合成运行时失败')).toBeNull())
})

it.each([false, true])('被外部删除的停用 MCP 另存草稿时保留停用状态（本页刚停用=%s）', async (toggleFirst) => {
  if (toggleFirst) mcpApi.listMcpServers.mockResolvedValue({ servers: [server({ enabled: true })] })
  render(<McpTab notify={vi.fn()} />)
  await screen.findByLabelText('服务名称')
  fill('服务名称', '恢复的停用连接')
  if (toggleFirst) {
    mcpApi.setMcpServerEnabled.mockResolvedValue(server({ revision: revision('b') }))
    await act(async () => fireEvent.click(screen.getByRole('switch', { name: '启用此 MCP 服务' })))
    expect(mcpApi.setMcpServerEnabled).toHaveBeenCalledWith('local', false, {}, revision('a'))
  }
  mcpApi.listMcpServers.mockResolvedValue({ servers: [] })
  await act(async () => publishSettingsChange('mcp'))
  await screen.findByText('这个连接已在其他地方移除')
  fireEvent.click(screen.getByRole('button', { name: '另存为新连接' }))
  expect(screen.queryByRole('button', { name: '保存并启用' })).toBeNull()
  mcpApi.createMcpServer.mockResolvedValue(server({ connection_id: 'restored', name: '恢复的停用连接' }))
  fireEvent.click(screen.getByRole('button', { name: '保存配置' }))
  await waitFor(() => expect(mcpApi.createMcpServer).toHaveBeenCalledOnce())
  expect(mcpApi.createMcpServer.mock.calls[0][0]).toMatchObject({ enabled: false, expected_revision: 'missing',
    creation_id: expect.stringMatching(/^mcp-[a-f0-9]{32}$/) })
})

it('新模型连接回包中断后按固定编号找回已保存结果，仍保留输入以供核对', async () => {
  providerApi.readProviderCatalog.mockResolvedValue(catalog([]))
  providerApi.saveProviderProfile.mockImplementation(async input => {
    providerApi.readProviderCatalog.mockResolvedValue(catalog([profile({ provider_id: input.providerId, base_url: input.baseUrl, model: input.model })]))
    throw new Error('响应途中断开')
  })
  render(<ModelServicesTab {...props()} />)
  await screen.findByText('添加第一个模型服务')
  fill(T.settings.baseUrl, 'https://new.invalid/v1')
  fill(T.settings.apiKey, 'recoverable-draft-secret')
  fireEvent.change(screen.getByPlaceholderText('例如 deepseek-chat'), { target: { value: 'new-model' } })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await screen.findByText('已找到这份草稿对应的连接。上次请求的结果可能已保存，请核对最新配置后继续。')
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('recoverable-draft-secret')
  expect(screen.getByRole('button', { name: T.settings.saveService })).toBeDisabled()
  expect(providerApi.saveProviderProfile.mock.calls[0][0]).toMatchObject({ expectedRevision: 'missing', providerId: expect.stringMatching(/^provider-[a-f0-9]{32}$/) })
  expect(providerApi.saveProviderProfile).toHaveBeenCalledTimes(1)
  fireEvent.click(screen.getByRole('button', { name: '放弃草稿，使用最新配置' }))
  expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('')
})

it('其他窗口完成 MCP 重连后，后台刷新清除过期的待应用反馈', async () => {
  render(<McpTab notify={vi.fn()} />)
  await screen.findByLabelText('服务名称')
  fill('服务名称', '待应用连接')
  const pending = server({ name: '待应用连接', revision: revision('b'), configuration_status: 'pending', apply_error: '临时应用失败' })
  mcpApi.updateMcpServer.mockResolvedValue(pending)
  await act(async () => fireEvent.click(screen.getByRole('button', { name: '保存配置' })))
  await screen.findByText('配置已保存，连接尚未应用。重新连接后可继续使用。')
  mcpApi.listMcpServers.mockResolvedValue({ servers: [{ ...pending, applied_revision: revision('b'), configuration_status: 'applied', apply_error: null }] })
  await act(async () => publishSettingsChange('mcp'))
  expect(screen.queryByText('配置已保存，连接尚未应用。重新连接后可继续使用。')).toBeNull()
  expect(screen.queryByText('临时应用失败')).toBeNull()
  expect(screen.getByLabelText('服务名称')).toHaveValue('待应用连接')
})

it('新 MCP 连接回包中断后不重复创建，读取后保留待核对草稿', async () => {
  mcpApi.listMcpServers.mockResolvedValue({ servers: [] })
  mcpApi.createMcpServer.mockImplementation(async body => {
    mcpApi.listMcpServers.mockResolvedValue({ servers: [server({ connection_id: body.creation_id, name: body.name, command: body.command })] })
    throw new Error('响应途中断开')
  })
  render(<McpTab notify={vi.fn()} />)
  await screen.findByText('还没有连接 MCP 服务')
  fireEvent.click(screen.getByRole('button', { name: '添加服务' }))
  fill('服务名称', '新工具')
  fireEvent.change(screen.getByLabelText(/启动程序/), { target: { value: 'fixture' } })
  fireEvent.click(screen.getByRole('button', { name: '保存并启用' }))
  await screen.findByText('已找到这份草稿对应的连接。请核对最新配置后继续，上次请求可能已经保存。')
  expect(mcpApi.createMcpServer).toHaveBeenCalledTimes(1)
  expect(mcpApi.createMcpServer.mock.calls[0][0]).toMatchObject({ expected_revision: 'missing', creation_id: expect.stringMatching(/^mcp-[a-f0-9]{32}$/) })
  expect(screen.getByLabelText('服务名称')).toHaveValue('新工具')
  expect(screen.getByRole('button', { name: '保存配置' })).toBeDisabled()
})

import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AgentSession } from '../../api/agent'
import type { ProviderProfileView } from '../../api/providerSettings'
import Root from '../Root'
import { T } from '../strings'

const state = vi.hoisted(() => ({ profiles: [] as ProviderProfileView[], rebind: vi.fn(), refresh: vi.fn() }))
const api = vi.hoisted(() => ({ readProviderSettings: vi.fn(), readProviderCatalog: vi.fn(), saveProviderProfile: vi.fn(),
  activateProviderProfile: vi.fn(), deleteProviderProfile: vi.fn(), fetchProviderModels: vi.fn(), testProviderModel: vi.fn(),
  testProviderModelsLiveness: vi.fn(), clearVerifiedRoutes: vi.fn(), saveProviderSettings: vi.fn() }))
vi.mock('../../api/providerSettings', () => api)
vi.mock('../../hooks/useRuntimeStatus', () => ({ useRuntimeStatus: () => ({ runtime: { ready: true, connection: 'online' } }) }))
vi.mock('../../hooks/useProjectWorkspace', () => ({ useProjectWorkspace: () => ({ selectedProjectId: null, snapshot: null, bindings: [], refresh: state.refresh }) }))
vi.mock('../../hooks/useChangeSets', () => ({ useChangeSets: () => ({ refresh: state.refresh }) }))
vi.mock('../lib/useConversations', () => ({ useConversations: () => ({ projects: [], refresh: state.refresh }) }))
const session = { session_id: 'old-session', capabilities: { extensions: {
  'x-zhimeng.provider-preference': { config: { provider_id: 'old', model: 'old-selected', reasoning_effort: 'high' } },
} } } as unknown as AgentSession
vi.mock('../../hooks/useAgentConversation', () => ({ AgentBusyError: class extends Error {}, useAgentConversation: () => ({
  session, sessions: [session], activities: [], messages: [], activeRun: null, rebindProvider: state.rebind,
}) }))
vi.mock('../Sidebar', () => ({ Sidebar: ({ onOpenSettings }: { onOpenSettings: () => void }) => <button onClick={onOpenSettings}>打开设置</button> }))
vi.mock('../chat/ChatView', async () => {
  const { ModelControls } = await import('../chat/models')
  return { ChatView: (props: { provider: never; onProviderChanged: () => Promise<void>; onOpenSettings: () => void; notify: never }) =>
    <ModelControls {...props} session={session} disabled={false} onSessionProviderChanged={state.rebind} /> }
})
vi.mock('../panel/Files', () => ({ FilesPanel: () => null }))
vi.mock('../panel/Changes', () => ({ ChangesPanel: () => null }))
vi.mock('../folder/FolderPicker', () => ({ FolderPicker: () => null }))

function catalog() { return { schema: 'zhimeng.provider-catalog.v1', providers: state.profiles, active_provider_id: state.profiles.find((p) => p.active)?.provider_id } }
function profile(id: string, active: boolean): ProviderProfileView {
  return { provider_id: id, name: id === 'main' ? '默认服务' : '旧服务', base_url: `https://${id}.example/v1`,
    model: `${id}-default`, selected_models: [`${id}-default`, `${id}-selected`], active, configured: true, api_key_set: true,
    wire_format: 'chat', reasoning_effort: 'auto', accepted_reported_models: [], context_window: '256k' }
}
beforeEach(() => {
  vi.clearAllMocks()
  localStorage.clear()
  state.profiles = [profile('main', true), profile('old', false)]
  state.rebind.mockResolvedValue(undefined)
  api.readProviderCatalog.mockImplementation(async () => catalog())
  api.readProviderSettings.mockImplementation(async () => ({ schema: 'zhimeng.provider-settings.v1', ...state.profiles.find((p) => p.active), source: 'settings' }))
  api.saveProviderProfile.mockImplementation(async (input) => {
    state.profiles = state.profiles.map((p) => p.provider_id === input.providerId ? { ...p, name: input.name, model: input.model } : p)
    return { catalog: catalog(), profile: state.profiles.find((p) => p.provider_id === input.providerId) }
  })
  api.activateProviderProfile.mockImplementation(async (id) => { state.profiles = state.profiles.map((p) => ({ ...p, active: p.provider_id === id })); return {} })
  api.deleteProviderProfile.mockImplementation(async (id) => { state.profiles = state.profiles.filter((p) => p.provider_id !== id); return catalog() })
})
afterEach(cleanup)
async function openSettings() {
  render(<Root />)
  await screen.findByRole('option', { name: 'old-selected' })
  await act(async () => {
    fireEvent.click(screen.getByRole('button', { name: '打开设置' }))
    await vi.dynamicImportSettled()
  })
  await screen.findByRole('button', { name: /^默认服务/ })
}

it('编辑非默认服务只刷新目录，当前对话保留原来的模型与强度', async () => {
  await openSettings()
  fireEvent.click(screen.getByRole('button', { name: /^旧服务/ }))
  fireEvent.click(screen.getByRole('button', { name: '更多设置' }))
  expect(screen.getByLabelText(T.settings.name)).toBeVisible()
  fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: '改过名字的旧服务' } })
  fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
  await screen.findByRole('button', { name: /^改过名字的旧服务/ })
  await waitFor(() => expect(screen.getByRole('button', { name: T.settings.saveService })).toBeEnabled())
  await userEvent.setup().click(screen.getByRole('button', { name: '返回工作台' }))
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-selected')
  expect(screen.getByRole('combobox', { name: T.agent.effortPick })).toHaveValue('high')
  expect(state.rebind).not.toHaveBeenCalled()
  expect(state.profiles.find((p) => p.active)?.provider_id).toBe('main')
})

it('设为默认仅更改新对话默认，已有对话不会重新绑定默认模型', async () => {
  await openSettings()
  fireEvent.click(screen.getByRole('button', { name: /^旧服务/ }))
  fireEvent.click(screen.getByRole('button', { name: '设为默认' }))
  await waitFor(() => expect(state.profiles.find((p) => p.active)?.provider_id).toBe('old'))
  await waitFor(() => expect(within(screen.getByRole('button', { name: /^旧服务/ })).getByText('新对话默认')).toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '返回工作台' }))
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-selected')
  expect(state.rebind).not.toHaveBeenCalled()
})

it('删除本会话使用的非默认服务后显示缺失，用户能直接选择另一服务恢复', async () => {
  await openSettings()
  fireEvent.click(screen.getByRole('button', { name: /^旧服务/ }))
  fireEvent.click(screen.getByRole('button', { name: T.settings.deleteService }))
  fireEvent.click(within(screen.getByRole('alertdialog')).getByRole('button', { name: T.common.delete }))
  await waitFor(() => expect(screen.queryByRole('button', { name: /^旧服务/ })).not.toBeInTheDocument())
  fireEvent.click(screen.getByRole('button', { name: '返回工作台' }))
  expect(screen.getByText('原模型服务已删除，请重新选择模型')).toBeInTheDocument()
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-selected')
  expect(state.rebind).not.toHaveBeenCalled()
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), JSON.stringify(['main', 'main-selected']))
  await waitFor(() => expect(state.rebind).toHaveBeenCalledWith({ provider_id: 'main', model: 'main-selected', reasoning_effort: 'high' }))
})

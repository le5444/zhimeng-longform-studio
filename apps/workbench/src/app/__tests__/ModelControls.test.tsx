import { cleanup, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import type { AgentSession } from '../../api/agent'
import { ModelControls } from '../chat/models'
import { T } from '../strings'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'

const save = vi.hoisted(() => vi.fn().mockResolvedValue({}))
const saveProfile = vi.hoisted(() => vi.fn().mockResolvedValue({}))
vi.mock('../../api/providerSettings', () => ({ saveProviderSettings: save, saveProviderProfile: saveProfile }))
afterEach(cleanup)
beforeEach(() => { vi.clearAllMocks(); save.mockResolvedValue({}); saveProfile.mockResolvedValue({}) })

function boundSession(model = 'old-model', reasoningEffort = 'high', providerId = 'old-provider'): AgentSession {
  return { session_id: 'session-old', capabilities: { extensions: {
    'x-zhimeng.provider-preference': { config: { provider_id: providerId, model, reasoning_effort: reasoningEffort } },
  } } } as unknown as AgentSession
}

it('回到旧对话时显示该会话实际绑定的模型和强度，而不是全局默认', () => {
  const props = { provider: { settings: { configured: true, provider_id: 'new-provider', model: 'new-model', reasoning_effort: 'auto' } } as never,
    session: boundSession(), disabled: false, onProviderChanged: vi.fn(), onOpenSettings: vi.fn(), notify: vi.fn() }
  const view = render(<ModelControls {...props} />)
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-model')
  expect(screen.getByRole('combobox', { name: T.agent.effortPick })).toHaveValue('high')
  view.rerender(<ModelControls {...props} session={boundSession('another-model', 'ultra')} />)
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('another-model')
  expect(screen.getByRole('combobox', { name: T.agent.effortPick })).toHaveValue('ultra')
})

function sessionProps() {
  return { provider: {
    settings: { configured: true, provider_id: 'new-provider', model: 'new-model', reasoning_effort: 'auto', context_window: '1m', selected_models: ['new-model'] },
    catalog: { providers: [{ provider_id: 'old-provider', name: '旧服务', model: 'service-default', selected_models: ['old-model', 'other-old-model'],
      reasoning_effort: 'auto', context_window: '256k', wire_format: 'responses', wire_format_explicit: false, accepted_reported_models: [] }] },
    reload: vi.fn().mockResolvedValue(undefined),
  } as never, session: boundSession(), disabled: false, onSessionProviderChanged: vi.fn().mockResolvedValue(undefined),
  onProviderChanged: vi.fn().mockResolvedValue(undefined), onOpenSettings: vi.fn(), notify: vi.fn() }
}

it('旧对话换模型或强度只改自己的供应商绑定，成功静默，不改全局默认', async () => {
  const props = sessionProps()
  render(<ModelControls {...props} />)
  const user = userEvent.setup()
  // 同一服务继续保留原模型，其他服务可在目录准备好后直接选择。
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-model')
  await user.selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), 'other-old-model')
  await waitFor(() => expect(props.onSessionProviderChanged).toHaveBeenCalledWith({ provider_id: 'old-provider', model: 'other-old-model', reasoning_effort: 'high' }))
  await user.selectOptions(screen.getByRole('combobox', { name: T.agent.effortPick }), 'low')
  await waitFor(() => expect(props.onSessionProviderChanged).toHaveBeenCalledWith({ provider_id: 'old-provider', model: 'old-model', reasoning_effort: 'low' }))
  expect(save).not.toHaveBeenCalled()
  expect(saveProfile).not.toHaveBeenCalled()
  expect(props.onProviderChanged).not.toHaveBeenCalled()
  expect(props.notify).not.toHaveBeenCalled()
})

it('当前会话换模型失败会提示，界面仍显示原模型', async () => {
  const props = sessionProps()
  props.onSessionProviderChanged.mockRejectedValueOnce(new Error('该服务没有这个模型'))
  render(<ModelControls {...props} />)
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), 'other-old-model')
  await waitFor(() => expect(props.notify).toHaveBeenCalledWith('danger', '该服务没有这个模型'))
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('old-model')
  expect(within(screen.getByRole('group', { name: '当前模型服务' })).getByText('旧服务', { selector: 'span' })).toBeVisible()
  expect(save).not.toHaveBeenCalled()
})

it('同名模型切换服务后同时显示正确服务名，仍只更新当前会话', async () => {
  const props = sessionProps()
  const provider = props.provider as unknown as ProviderSettingsApi
  const old = provider.catalog!.providers[0]
  old.selected_models = ['shared-model']
  provider.catalog!.providers.push({ ...old, provider_id: 'other-provider', name: '另一条线路' })
  const view = render(<ModelControls {...props} session={boundSession('shared-model')} />)
  const identity = () => screen.getByRole('group', { name: '当前模型服务' })
  expect(within(identity()).getByText('旧服务', { selector: 'span' })).toBeVisible()
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), JSON.stringify(['other-provider', 'shared-model']))
  await waitFor(() => expect(props.onSessionProviderChanged).toHaveBeenCalledWith({ provider_id: 'other-provider', model: 'shared-model', reasoning_effort: 'high' }))
  view.rerender(<ModelControls {...props} session={boundSession('shared-model', 'high', 'other-provider')} />)
  expect(within(identity()).getByText('另一条线路', { selector: 'span' })).toBeVisible()
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue('shared-model')
  expect(save).not.toHaveBeenCalled()
  expect(saveProfile).not.toHaveBeenCalled()
})

it('旧对话服务尚未读取或已删除时不冒用全局默认的名称', () => {
  const props = sessionProps()
  const provider = props.provider as unknown as ProviderSettingsApi
  provider.settings!.provider_name = '新对话默认线路'
  const catalog = provider.catalog
  provider.catalog = null
  const view = render(<ModelControls {...props} />)
  expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toBeDisabled()
  const identity = () => screen.getByRole('group', { name: '当前模型服务' })
  expect(within(identity()).getByText('服务名称未读取', { selector: 'span' })).toBeVisible()
  expect(identity()).not.toHaveTextContent('新对话默认线路')
  provider.catalog = { ...catalog!, providers: [] }
  view.rerender(<ModelControls {...props} />)
  expect(within(identity()).getByText('已删除的服务', { selector: 'span' })).toBeVisible()
  expect(identity()).not.toHaveTextContent('新对话默认线路')
})

it('失效服务的操作聚焦模型选择，只有选中后才更新当前会话，忙碌时不能再切换', async () => {
  const props = sessionProps()
  const provider = props.provider as unknown as ProviderSettingsApi
  provider.catalog!.providers[0].provider_id = 'available-provider'
  const view = render(<ModelControls {...props} />)
  const user = userEvent.setup()
  const select = screen.getByRole('combobox', { name: T.agent.modelPick })
  // 不支持原生 picker 的浏览器仍可用键盘选择。
  Object.defineProperty(select, 'showPicker', { value: () => { throw new Error('unsupported') }, configurable: true })
  await user.click(screen.getByRole('button', { name: '选择可用模型' }))
  expect(select).toHaveFocus()
  expect(select).toHaveValue('old-model')
  expect(select).toHaveAccessibleDescription(expect.stringContaining('原模型服务已删除'))
  expect(props.onSessionProviderChanged).not.toHaveBeenCalled()
  let finish!: () => void
  props.onSessionProviderChanged.mockImplementationOnce(() => new Promise<void>(resolve => { finish = resolve }))
  await user.selectOptions(select, JSON.stringify(['available-provider', 'other-old-model']))
  expect(props.onSessionProviderChanged).toHaveBeenCalledWith({ provider_id: 'available-provider', model: 'other-old-model', reasoning_effort: 'high' })
  expect(screen.getByRole('button', { name: '选择可用模型' })).toBeDisabled()
  finish()
  await waitFor(() => expect(select).toBeEnabled())
  view.rerender(<ModelControls {...props} disabled />)
  expect(screen.getByRole('button', { name: '选择可用模型' })).toBeDisabled()
  expect(save).not.toHaveBeenCalled()
  expect(saveProfile).not.toHaveBeenCalled()
})

it('没有可替代模型时提供配置入口，不自动改写旧会话', async () => {
  const props = sessionProps()
  const provider = props.provider as unknown as ProviderSettingsApi
  provider.catalog!.providers = []
  render(<ModelControls {...props} />)
  await userEvent.setup().click(screen.getByRole('button', { name: '配置模型服务' }))
  expect(props.onOpenSettings).toHaveBeenCalledOnce()
  expect(props.onSessionProviderChanged).not.toHaveBeenCalled()
})

it('上下文窗口读写旧对话所属服务，保留自动协议与该会话模型，不激活或改写其他服务', async () => {
  const props = sessionProps()
  render(<ModelControls {...props} />)
  expect(screen.getByRole('combobox', { name: T.agent.contextWindowPick })).toHaveValue('256k')
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.contextWindowPick }), '1m')
  await waitFor(() => expect(saveProfile).toHaveBeenCalledWith({ providerId: 'old-provider', name: '旧服务', model: 'service-default',
    selectedModels: ['old-model', 'other-old-model'], reasoningEffort: 'auto', wireFormat: 'auto', acceptedReportedModels: [], contextWindow: '1m', makeActive: false }))
  expect(props.onSessionProviderChanged).not.toHaveBeenCalled()
  expect(save).not.toHaveBeenCalled()
  expect(props.onProviderChanged).not.toHaveBeenCalled()
  expect(props.notify).not.toHaveBeenCalled()
})

it('容量已保存但目录刷新失败时如实报告，不重写会话模型', async () => {
  const props = sessionProps()
  const provider = props.provider as unknown as ProviderSettingsApi
  provider.reload = vi.fn().mockRejectedValue(new Error('目录暂不可用'))
  render(<ModelControls {...props} />)
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.contextWindowPick }), '1m')
  await waitFor(() => expect(props.notify).toHaveBeenCalledWith('danger', expect.stringContaining('历史容量已保存，但模型列表刷新失败')))
  expect(props.onSessionProviderChanged).not.toHaveBeenCalled()
})

it('新对话默认模型保存后的刷新或绑定失败也提示，不伪装切换成功', async () => {
  const props = { provider: { settings: { configured: true, model: 'a', selected_models: ['b'] } } as never, disabled: false,
    onProviderChanged: vi.fn().mockRejectedValue(new Error('读取模型设置失败')), onOpenSettings: vi.fn(), notify: vi.fn() }
  render(<ModelControls {...props} />)
  await userEvent.setup().selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), 'b')
  await waitFor(() => expect(props.notify).toHaveBeenCalledWith('danger', '读取模型设置失败'))
})

it('上下文窗口是原生下拉：本地切换并保存，不跳设置；忙碌时禁用；成功不打扰', async () => {
  const props = { provider: { settings: { configured: true, model: 'model', context_window: '256k' } } as never, disabled: false, onProviderChanged: vi.fn().mockResolvedValue(undefined), onOpenSettings: vi.fn(), notify: vi.fn() }
  const view = render(<ModelControls {...props} />)
  const user = userEvent.setup()
  await user.selectOptions(screen.getByRole('combobox', { name: T.agent.contextWindowPick }), '1m')
  await waitFor(() => expect(save).toHaveBeenCalledWith({ contextWindow: '1m' }))
  expect(props.onOpenSettings).not.toHaveBeenCalled()
  expect(props.notify).not.toHaveBeenCalled()
  await waitFor(() => expect(props.onProviderChanged).toHaveBeenCalledOnce())
  view.rerender(<ModelControls {...props} disabled />)
  expect(screen.getByRole('combobox', { name: T.agent.contextWindowPick })).toBeDisabled()
})

it('换模型和换思考强度也是原生下拉，选中就存', async () => {
  const props = { provider: { settings: { configured: true, model: 'a-model', selected_models: ['b-model'] } } as never, disabled: false, onProviderChanged: vi.fn().mockResolvedValue(undefined), onOpenSettings: vi.fn(), notify: vi.fn() }
  render(<ModelControls {...props} />)
  const user = userEvent.setup()
  await user.selectOptions(screen.getByRole('combobox', { name: T.agent.modelPick }), 'b-model')
  await waitFor(() => expect(save).toHaveBeenCalledWith({ model: 'b-model' }))
  await user.selectOptions(screen.getByRole('combobox', { name: T.agent.effortPick }), 'high')
  await waitFor(() => expect(save).toHaveBeenCalledWith({ reasoningEffort: 'high' }))
})

/**
 * 设置页（9-29 回炉重造：整页，左边分组目录）：模型服务列表与表单、密钥只进不出、获取 / 测试 / 保存 / 切换 / 删除；
 * 外观（主题跟随系统 / 亮 / 暗、界面字号、Agent 改文件时怎么存）、记忆那一节里的项目说明、关于。接口全部 mock，不启动服务。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import type { ComponentProps } from 'react'
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'
import type { ProviderCatalogView, ProviderLivenessResponse, ProviderProfileView } from '../../api/providerSettings'
import { SettingsView } from '../settings/Settings'
import { ST } from '../settings/settings.text'
import { S } from '../settings/settingsText'
import { T } from '../strings'
import { makeFile, makeSnapshot } from './fixtures'

// Open real optional sections; connection fields remain visible throughout.
function modelPane(name: '连接' | '模型与默认值' | '连接验证' | '高级选项') {
  if (document.querySelector('dialog[open]')) return
  const title = name === '模型与默认值' ? '管理可选模型' : name === '连接验证' ? '连接诊断' : '更多设置'
  const button = screen.getByRole('button', { name: title })
  if (button.getAttribute('aria-expanded') !== 'true') fireEvent.click(button)
  expect(button).toHaveAttribute('aria-expanded', 'true')
}

type SettingsPageProps = ComponentProps<typeof SettingsView>

const api = vi.hoisted(() => ({
  readProviderCatalog: vi.fn(),
  saveProviderProfile: vi.fn(),
  activateProviderProfile: vi.fn(),
  deleteProviderProfile: vi.fn(),
  fetchProviderModels: vi.fn(),
  testProviderModelsLiveness: vi.fn(),
  testProviderModel: vi.fn(),
  clearVerifiedRoutes: vi.fn(),
  revealProviderKey: vi.fn(),
  patchProviderModelOverride: vi.fn(),
}))

vi.mock('../../api/providerSettings', () => api)

const runtimeApi = vi.hoisted(() => ({ fetchRuntimeSnapshot: vi.fn() }))
vi.mock('../../api/runtime', async (importOriginal) => ({
  ...await importOriginal<typeof import('../../api/runtime')>(),
  ...runtimeApi,
}))

const memoryApi = vi.hoisted(() => ({ readUserMemory: vi.fn(), saveUserMemory: vi.fn(), saveMemoryEnabled: vi.fn() }))
vi.mock('../../api/userMemory', () => memoryApi)

afterEach(cleanup)
beforeEach(() => sessionStorage.clear())
// jsdom has no top-layer dialog implementation; browser acceptance covers it.
beforeAll(() => Object.defineProperty(HTMLDialogElement.prototype, 'showModal', { configurable: true, value() { this.open = true } }))
afterAll(() => { Reflect.deleteProperty(HTMLDialogElement.prototype, 'showModal') })

function profile(over: Partial<ProviderProfileView> = {}): ProviderProfileView {
  return {
    provider_id: 'p-main',
    name: '主力',
    base_url: 'https://relay.example/v1',
    model: 'gpt-5',
    selected_models: ['gpt-5', 'glm-5'],
    reasoning_effort: 'medium',
    wire_format: 'chat',
    accepted_reported_models: [],
    auth_type: 'bearer',
    extra_header_names: [],
    context_window: '256k',
    api_key_set: true,
    configured: true,
    active: true,
    ...over,
  }
}

const spare = (): ProviderProfileView => profile({
  provider_id: 'p-spare',
  name: '备用',
  base_url: 'https://spare.example/v1',
  model: 'deepseek-v4',
  selected_models: ['deepseek-v4'],
  api_key_set: false,
  active: false,
})

function catalog(providers: ProviderProfileView[]): ProviderCatalogView {
  return {
    schema: 'zhimeng.provider-catalog.v1',
    active_provider_id: providers.find((item) => item.active)?.provider_id ?? null,
    providers,
  }
}

function saveResult(saved: ProviderProfileView, list: ProviderProfileView[]) {
  return {
    catalog: catalog(list),
    profile: saved,
    active_settings: {
      schema: 'zhimeng.provider-settings.v1',
      base_url: saved.base_url,
      model: saved.model,
      api_key_set: true,
      source: 'settings',
      configured: true,
    },
  }
}

function makeProps(over: Partial<SettingsPageProps> = {}): SettingsPageProps {
  return {
    section: 'models',
    onSection: vi.fn(),
    onClose: vi.fn(),
    projectSnapshot: null,
    onOpenProjectFile: vi.fn(),
    runtime: {
      connection: 'online',
      ready: true,
      serviceStatus: 'ready',
      runStatus: null,
      version: '0.9.3',
      detail: '',
      checkedAt: '',
    },
    theme: 'light',
    onTheme: vi.fn(),
    provider: { settings: null, catalog: null, state: 'ready', reload: vi.fn(async () => undefined) },
    onProviderChanged: vi.fn(async () => undefined),
    notify: vi.fn(),
    askConfirm: vi.fn(),
    ...over,
  }
}

/** 列表里那一套的整块（名字按钮 + 用这套 / 删除这套）。 */
function serviceItem(name: string): HTMLElement {
  const pick = screen.getByRole('button', { name: new RegExp(`^${name}`) })
  const item = pick.closest('li')
  if (!item) throw new Error('service item not found')
  return item
}

describe('设置页 · 模型服务', () => {
  it('首次目录读取失败后重试会打开默认服务，并保留正常编辑能力', async () => {
    api.readProviderCatalog.mockRejectedValueOnce(new Error('network unavailable')).mockResolvedValue(catalog([profile()]))
    render(<SettingsView {...makeProps()} />)
    fireEvent.click(await screen.findByRole('button', { name: T.common.retry }))
    await screen.findByLabelText(T.settings.baseUrl)
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('主力')
    expect(screen.getByRole('button', { name: T.settings.saveService })).toBeEnabled()
  })

  it('基础接入同页可见，展开与搜索高级设置保留草稿', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    render(<SettingsView {...props} />)
    const address = await screen.findByLabelText(T.settings.baseUrl)
    fireEvent.change(address, { target: { value: 'https://draft.example/v1' } })
    expect(screen.getByLabelText(T.settings.apiKey)).toBeVisible()
    expect(screen.getByRole('combobox', { name: /^模型/ })).toBeVisible()
    expect(screen.queryByRole('tab')).toBeNull()
    modelPane('模型与默认值')
    expect(address).toBeVisible()
    expect(address).toHaveValue('https://draft.example/v1')
    await user.type(screen.getByRole('textbox', { name: S.search }), 'headers')
    await user.click(screen.getByRole('button', { name: /接口与高级连接选项/ }))
    expect(screen.getByLabelText(T.settings.authType)).toBeVisible()
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
    expect(address).toBeVisible()
    expect(address).toHaveValue('https://draft.example/v1')
  })

  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset()
    api.readProviderCatalog.mockResolvedValue(catalog([profile(), spare()]))
  })

  it('列出已保存的模型服务，使用中的那套默认打开在右边表单里，密钥只显示「已保存」', async () => {
    render(<SettingsView {...makeProps()} />)

    const main = await screen.findByRole('button', { name: /^主力/ })
    expect(main).toHaveAttribute('aria-pressed', 'true')
    expect(within(main).getByText(T.settings.inUse)).toBeInTheDocument()
    expect(within(main).getByText('gpt-5')).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /^备用/ })).toHaveAttribute('aria-pressed', 'false')
    expect(screen.queryByRole('button', { name: T.settings.useThis })).toBeNull()
    expect(screen.getAllByRole('button', { name: T.settings.deleteService })).toHaveLength(1)

    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('主力')
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://relay.example/v1')
    modelPane('连接')
    expect(screen.getByText(T.settings.apiKeySet)).toBeInTheDocument()
    modelPane('连接')
    const keyInput = screen.getByLabelText(T.settings.apiKey)
    expect(keyInput).toHaveAttribute('type', 'password')
    expect(keyInput).toHaveValue('')

    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeChecked()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'glm-5' })).toBeChecked()
    modelPane('模型与默认值')
    const radios = screen.getAllByRole('radio', { name: T.settings.activeModel })
    expect(radios).toHaveLength(2)
    expect(radios[0]).toBeChecked()
    modelPane('高级选项')
    expect(screen.getByLabelText(new RegExp(`^${T.settings.effort}`))).toHaveValue('medium')
    expect(screen.getByRole('button', { name: '更多设置' })).toHaveAttribute('aria-expanded', 'true')
  })

  it('只填地址、密钥和模型即可保存，目录失败不阻挡，名称从域名生成', async () => {
    api.readProviderCatalog.mockResolvedValue(catalog([]))
    api.fetchProviderModels.mockRejectedValue(new Error('目录不可用'))
    const saved = profile({ name: 'gateway.example', base_url: 'https://gateway.example/v1', model: 'deepseek-chat', selected_models: ['deepseek-chat'] })
    api.saveProviderProfile.mockResolvedValue(saveResult(saved, [saved]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByLabelText(T.settings.baseUrl)
    fireEvent.change(screen.getByLabelText(T.settings.baseUrl), { target: { value: 'https://gateway.example/v1' } })
    fireEvent.change(screen.getByLabelText(T.settings.apiKey), { target: { value: 'test-key' } })
    fireEvent.click(screen.getByRole('button', { name: T.settings.fetchModels }))
    await screen.findByText('可以直接填写模型 ID 后保存。')
    const input = screen.getByRole('combobox', { name: /^模型/ })
    fireEvent.change(input, { target: { value: 'deep' } })
    fireEvent.change(input, { target: { value: ' deepseek-chat ' } })
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledWith(expect.objectContaining({ name: 'gateway.example', baseUrl: 'https://gateway.example/v1', apiKey: 'test-key', model: 'deepseek-chat', selectedModels: ['deepseek-chat'], wireFormat: 'auto' })))
    expect(api.testProviderModel).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: '更多设置' })).toHaveAttribute('aria-expanded', 'false')
    await waitFor(() => expect(input).toHaveValue('deepseek-chat'))
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('')
  })

  it('直接改模型保存失败保留草稿，重试不保存输入中途的半截名称', async () => {
    const saved = profile({ model: 'deepseek-chat', selected_models: ['gpt-5', 'glm-5', 'deepseek-chat'] })
    api.saveProviderProfile.mockRejectedValueOnce(new Error('连接中断')).mockResolvedValue(saveResult(saved, [saved, spare()]))
    const props = makeProps()
    render(<SettingsView {...props} />)
    const input = await screen.findByRole('combobox', { name: /^模型/ })
    fireEvent.change(input, { target: { value: 'deep' } })
    fireEvent.change(input, { target: { value: 'deepseek-chat' } })
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(props.notify).toHaveBeenCalledWith('danger', expect.stringContaining('连接中断')))
    expect(input).toHaveValue('deepseek-chat')
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(2))
    for (const [payload] of api.saveProviderProfile.mock.calls) expect(payload.selectedModels).toEqual(['gpt-5', 'glm-5', 'deepseek-chat'])
  })

  it('手填主模型后勾选额外模型不覆盖主模型，连接测试原位显示结果', async () => {
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'deepseek-chat', reply: 'ok', latency_ms: 500, wire_format: 'chat' })
    api.saveProviderProfile.mockResolvedValue(saveResult(profile(), [profile(), spare()]))
    render(<SettingsView {...makeProps()} />)
    const input = await screen.findByRole('combobox', { name: /^模型/ })
    fireEvent.change(input, { target: { value: 'deepseek-chat' } })
    modelPane('模型与默认值')
    fireEvent.click(screen.getByRole('checkbox', { name: 'glm-5' }))
    expect(input).toHaveValue('deepseek-chat')
    fireEvent.click(screen.getByRole('button', { name: '管理可选模型' }))
    fireEvent.click(screen.getByRole('button', { name: '测试连接' }))
    await screen.findByText(ST.testOk('0.5'))
    expect(screen.getByRole('button', { name: '连接诊断' })).toHaveAttribute('aria-expanded', 'false')
    expect(api.testProviderModel).toHaveBeenCalledWith(expect.objectContaining({ model: 'deepseek-chat' }))
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledWith(expect.objectContaining({ model: 'deepseek-chat', selectedModels: ['gpt-5', 'deepseek-chat'] })))
  })

  it('作者本人点「显示已存的密钥」才去取明文，再点就藏起来；没保存密钥的那套不给这个按钮', async () => {
    const user = userEvent.setup()
    api.revealProviderKey.mockResolvedValue({ api_key: 'sk-saved-secret' })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.showSavedKey }))
    expect(api.revealProviderKey).toHaveBeenCalledWith('p-main')
    expect(await screen.findByTestId('revealed-key')).toHaveTextContent('sk-saved-secret')

    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.hideSavedKey }))
    expect(screen.queryByTestId('revealed-key')).toBeNull()

    await user.click(screen.getByRole('button', { name: /^备用/ }))
    expect(screen.queryByRole('button', { name: T.settings.showSavedKey })).toBeNull()
  })

  it('切换服务后，迟到的密钥不能显示在另一套服务里', async () => {
    let finish!: (value: { api_key: string }) => void
    api.revealProviderKey.mockReturnValue(new Promise((resolve) => { finish = resolve }))
    api.readProviderCatalog.mockResolvedValue(catalog([profile(), { ...spare(), api_key_set: true }]))
    render(<SettingsView {...makeProps()} />)
    const user = userEvent.setup()
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.showSavedKey }))
    await user.click(screen.getByRole('button', { name: /^备用/ }))
    await act(async () => finish({ api_key: 'synthetic-first-service-key' }))
    expect(screen.queryByTestId('revealed-key')).toBeNull()
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('备用')
  })

  it('保存：密钥留空就不传；本来使用中的那套保存后还在用；成功后刷新当前模型并提示已保存', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.saveProviderProfile.mockResolvedValue(saveResult(profile({ name: '主力二号' }), [profile({ name: '主力二号' }), spare()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接')
    const nameInput = screen.getByLabelText(T.settings.name)
    await user.clear(nameInput)
    await user.type(nameInput, '主力二号')
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))

    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    expect(input.providerId).toBe('p-main')
    expect(input.name).toBe('主力二号')
    expect(input.baseUrl).toBe('https://relay.example/v1')
    expect(input.model).toBe('gpt-5')
    expect(input.selectedModels).toEqual(['gpt-5', 'glm-5'])
    expect(input.reasoningEffort).toBe('medium')
    expect(input.makeActive).toBe(false)
    expect('apiKey' in input).toBe(false)
    expect('extraHeaders' in input).toBe(false)

    await waitFor(() => expect(props.onProviderChanged).toHaveBeenCalledTimes(1))
    expect(props.notify).toHaveBeenCalledWith('ok', T.settings.saved)
    // 保存的回应里带着最新列表：左边列表和右边标题都按新名字显示，仍标「使用中」
    const renamed = await screen.findByRole('button', { name: /^主力二号/ })
    expect(within(renamed).getByText(T.settings.inUse)).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /^主力二号/ })).toBeInTheDocument()
  })

  it('配置已保存但对话刷新失败时，保留已保存事实并显示局部失败', async () => {
    const user = userEvent.setup()
    const props = makeProps({ onProviderChanged: vi.fn().mockRejectedValue(new Error('refresh unavailable')) })
    api.saveProviderProfile.mockResolvedValue(saveResult(profile({ name: '保存成功的服务' }), [profile({ name: '保存成功的服务' })]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: '保存成功的服务' } })
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(props.notify).toHaveBeenCalledWith('warn', expect.stringContaining(ST.savedRuntimeRefreshFailed)))
    expect(props.notify).not.toHaveBeenCalledWith('ok', expect.anything())
    expect(screen.getByRole('heading', { name: '保存成功的服务' })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: '放弃修改' })).toBeNull()
  })

  it('保存：作者填了新密钥才会传出去', async () => {
    const user = userEvent.setup()
    api.saveProviderProfile.mockResolvedValue(saveResult(profile(), [profile(), spare()]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.apiKey), 'sk-new-key')
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))

    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    expect(input.apiKey).toBe('sk-new-key')
    // 保存成功后密钥框清空
    modelPane('连接')
    await waitFor(() => expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue(''))
  })

  it('新增一套：拉模型、全选、保存；列表里已有使用中的，新的这套不抢', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.fetchProviderModels.mockResolvedValue(['alpha-1', 'beta-2'])
    const created = profile({ provider_id: 'p-new', name: '新线', base_url: 'https://new.example/v1', model: 'alpha-1', selected_models: ['alpha-1', 'beta-2'], active: false })
    api.saveProviderProfile.mockResolvedValue(saveResult(created, [profile(), spare(), created]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    await user.click(screen.getByRole('button', { name: T.settings.addService }))
    expect(screen.getByRole('heading', { name: ST.newServiceTitle })).toBeInTheDocument()
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('')
    modelPane('连接')
    expect(screen.getByText(T.settings.apiKeyUnset)).toBeInTheDocument()
    expect(screen.getByText(ST.noModelsYet)).toBeInTheDocument()

    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.name), '新线')
    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.baseUrl), 'https://new.example/v1')
    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.apiKey), 'sk-new')
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.fetchModels }))

    modelPane('模型与默认值')
    await screen.findByRole('checkbox', { name: 'alpha-1' })
    expect(api.fetchProviderModels).toHaveBeenCalledWith('https://new.example/v1', 'sk-new', {}, undefined, {
      authType: 'bearer', extraHeaders: undefined, modelsUrl: '',
    })
    expect(screen.getByText(new RegExp(T.settings.modelsFetched(2)))).toBeInTheDocument()
    expect(props.notify).not.toHaveBeenCalled()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'alpha-1' })).not.toBeChecked()

    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.selectAll }))
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'alpha-1' })).toBeChecked()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'beta-2' })).toBeChecked()
    // 默认用第一个
    modelPane('模型与默认值')
    expect(screen.getAllByRole('radio', { name: T.settings.activeModel })[0]).toBeChecked()

    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    expect(input.providerId).toMatch(/^provider-[a-f0-9]{32}$/)
    expect(input.expectedRevision).toBe('missing')
    expect(input.name).toBe('新线')
    expect(input.apiKey).toBe('sk-new')
    expect(input.selectedModels).toEqual(['alpha-1', 'beta-2'])
    expect(input.model).toBe('alpha-1')
    expect(input.makeActive).toBe(false)
    // 新建服务默认「自动」，而且照发 'auto'：以前不发这个字段，后端接口默认填成 chat，存成了「明确选了 Chat」。
    expect(input.wireFormat).toBe('auto')
    expect(input.authType).toBe('bearer')
    await waitFor(() => expect(props.notify).toHaveBeenCalledWith('ok', T.settings.saved))
  })

  it('一套都没保存过时，直接给一份空表单，保存的这套就用上', async () => {
    const user = userEvent.setup()
    api.readProviderCatalog.mockResolvedValue(catalog([]))
    const created = profile({ provider_id: 'p-first', name: '第一套', model: 'm-1', selected_models: ['m-1'] })
    api.saveProviderProfile.mockResolvedValue(saveResult(created, [created]))
    render(<SettingsView {...makeProps()} />)

    await screen.findByText('添加第一个模型服务')
    expect(screen.getByRole('heading', { name: ST.newServiceTitle })).toBeInTheDocument()
    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.name), '第一套')
    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.baseUrl), 'https://first.example/v1')
    modelPane('模型与默认值')
    await user.type(screen.getByLabelText(T.settings.manualModel), 'm-1')
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.addManual }))
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))

    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    // The backend assigns an empty catalog's first default atomically.
    expect(input.makeActive).toBe(false)
    expect(input.selectedModels).toEqual(['m-1'])
  })

  it('获取目录的反馈跟随连接，改密钥后不再显示旧目录成功', async () => {
    const user = userEvent.setup()
    api.fetchProviderModels.mockResolvedValue(['gpt-5', 'glm-5'])
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.fetchModels }))
    await screen.findByText(new RegExp(T.settings.modelsFetched(2)))
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.apiKey), { target: { value: 'different-test-key' } })
    expect(screen.queryByText(new RegExp(T.settings.modelsFetched(2)))).toBeNull()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeChecked()
  })

  it('模型结果筛选用工具验证事实，文字成功不算工具通过，筛选不改变勾选', async () => {
    const user = userEvent.setup()
    const saved = profile({ wire_format_explicit: false, verified_routes: { 'gpt-5': { wire_format: 'responses', tools: true, verified_at: '2026-10-09' } } })
    api.readProviderCatalog.mockResolvedValue(catalog([saved]))
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'glm-5', reply: 'ok', latency_ms: 10, wire_format: 'responses' })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    await user.selectOptions(screen.getByLabelText(ST.resultFilter), 'verified')
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeChecked()
    expect(screen.queryByRole('checkbox', { name: 'glm-5' })).toBeNull()
    modelPane('模型与默认值')
    await user.selectOptions(screen.getByLabelText(ST.resultFilter), 'pending')
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'glm-5' })).toBeChecked()
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.testModel }))
    await screen.findByText(ST.testOk('0.0'))
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'glm-5' })).toBeChecked()
    modelPane('模型与默认值')
    await user.selectOptions(screen.getByLabelText(ST.resultFilter), 'attention')
    await user.click(screen.getByRole('button', { name: ST.clearModelFilters }))
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeChecked()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'glm-5' })).toBeChecked()
    expect(screen.getByText(ST.verifiedModels(1, 2))).toBeInTheDocument()
  })

  it('模型列表拉不到时只是提醒，手动填一个模型名照样能加进来并勾上', async () => {
    const user = userEvent.setup()
    api.fetchProviderModels.mockRejectedValue(new Error('HTTP 404'))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.fetchModels }))
    modelPane('模型与默认值')
    await screen.findByText('可以直接填写模型 ID 后保存。')
    expect(screen.getByRole('alert')).toHaveTextContent('HTTP 404')

    modelPane('模型与默认值')
    await user.type(screen.getByLabelText(T.settings.manualModel), 'my-model')
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.addManual }))
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'my-model' })).toBeChecked()
    modelPane('模型与默认值')
    expect(screen.getByLabelText(T.settings.manualModel)).toHaveValue('')
    modelPane('模型与默认值')
    expect(screen.getAllByRole('radio', { name: T.settings.activeModel })).toHaveLength(3)
  })

  it('输入密钥立即更新状态，可以显示和隐藏，切换服务后重新隐藏', async () => {
    const user = userEvent.setup()
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    await user.click(screen.getByRole('button', { name: T.settings.addService }))
    modelPane('连接')
    const key = screen.getByLabelText(T.settings.apiKey)
    modelPane('连接')
    expect(screen.getByRole('button', { name: T.settings.showKey })).toBeDisabled()
    await user.type(key, 'sk-draft')
    expect(screen.queryByText(T.settings.apiKeyUnset)).not.toBeInTheDocument()
    modelPane('连接')
    expect(screen.getByText(T.settings.apiKeyDraft)).toBeInTheDocument()
    expect(key).toHaveAttribute('type', 'password')
    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.showKey }))
    expect(key).toHaveAttribute('type', 'text')
    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.hideKey }))
    expect(key).toHaveAttribute('type', 'password')
    modelPane('连接')
    await user.click(screen.getByRole('button', { name: T.settings.showKey }))
    await user.click(screen.getByRole('button', { name: '放弃修改' }))
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('')
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveAttribute('type', 'password')
  })

  it('获取模型使用未保存的认证、请求头和列表地址，不先保存也不调用生成', async () => {
    const user = userEvent.setup()
    api.fetchProviderModels.mockResolvedValue(['m-draft'])
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('高级选项')
    await user.selectOptions(screen.getByLabelText(T.settings.authType), 'x-api-key')
    modelPane('高级选项')
    await user.type(screen.getByLabelText(new RegExp(`^${T.settings.extraHeaders}`)), 'X-Client: draft')
    modelPane('高级选项')
    await user.type(screen.getByLabelText(new RegExp(`^${T.settings.modelsUrl}`)), 'https://relay.example/custom/models')
    modelPane('连接')
    await user.type(screen.getByLabelText(T.settings.apiKey), 'sk-new')
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.fetchModels }))
    modelPane('模型与默认值')
    await screen.findByRole('checkbox', { name: 'm-draft' })
    expect(api.fetchProviderModels).toHaveBeenCalledWith('https://relay.example/v1', 'sk-new', {}, 'p-main', {
      authType: 'x-api-key', extraHeaders: { 'X-Client': 'draft' }, modelsUrl: 'https://relay.example/custom/models',
    })
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
    expect(api.testProviderModel).not.toHaveBeenCalled()
    expect(api.testProviderModelsLiveness).not.toHaveBeenCalled()
  })

  it('没填的字段先提醒，不发保存请求', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    await user.clear(screen.getByLabelText(T.settings.baseUrl))
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    expect(props.notify).toHaveBeenCalledWith('danger', ST.needBaseUrl)
    await user.type(screen.getByLabelText(T.settings.baseUrl), 'https://relay.example/v1')
    modelPane('模型与默认值')
    await user.click(screen.getByRole('button', { name: T.settings.selectNone }))
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    expect(props.notify).toHaveBeenCalledWith('danger', ST.needModels)
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
  })

  it('上下文两档开关：默认 256K，切成 1M（最大）后保存带 contextWindow；界面没有「预估」', async () => {
    const user = userEvent.setup()
    api.saveProviderProfile.mockResolvedValue(saveResult(profile({ context_window: '1m' }), [profile({ context_window: '1m' }), spare()]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('高级选项')
    const select = screen.getByLabelText(new RegExp(`^${T.settings.contextWindow}`)) as HTMLSelectElement
    expect(select).toHaveValue('256k')
    expect(select).not.toHaveTextContent('预估')

    await user.selectOptions(select, '1m')
    expect(select).toHaveValue('1m')

    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    expect(input.contextWindow).toBe('1m')
  })

  it('自定义请求头写错格式时不保存并提醒；写对了按「名字: 值」传出去', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.saveProviderProfile.mockResolvedValue(saveResult(profile({ extra_header_names: ['X-Token'] }), [profile({ extra_header_names: ['X-Token'] }), spare()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('高级选项')
    const textarea = screen.getByPlaceholderText(T.settings.extraHeadersPlaceholder)
    fireEvent.change(textarea, { target: { value: '这一行没有冒号' } })
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    expect(props.notify).toHaveBeenCalledWith('danger', ST.extraHeadersBadLine('这一行没有冒号'))
    expect(api.saveProviderProfile).not.toHaveBeenCalled()

    fireEvent.change(textarea, { target: { value: 'X-Token: abc\n\nX-Region：cn' } })
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    const input = api.saveProviderProfile.mock.calls[0][0] as Record<string, unknown>
    expect(input.extraHeaders).toEqual({ 'X-Token': 'abc', 'X-Region': 'cn' })
    // 保存后只显示已保存的请求头名字，值不回显
    await screen.findByText(new RegExp(ST.savedHeaderNames(['X-Token'])))
    modelPane('高级选项')
    expect(screen.getByPlaceholderText(T.settings.extraHeadersPlaceholder)).toHaveValue('')
  })

  it('把勾选的都测一遍：每个模型一个结果，汇总能用几个和最快的', async () => {
    const user = userEvent.setup()
    const response: ProviderLivenessResponse = {
      schema: 'zhimeng.provider-model-liveness.v1',
      tested: 2,
      usable_count: 1,
      usable_models: ['gpt-5'],
      fastest_model: 'gpt-5',
      results: [
        { model: 'gpt-5', status: 'ok', usable: true, label: '', note: '首字 0.8 秒', first_byte_ms: 800, elapsed_ms: 1200, reply: '连接成功', tools: 'ok', wire_format: 'chat' },
        { model: 'glm-5', status: 'auth', usable: false, label: '', note: '', first_byte_ms: null, elapsed_ms: 300, reply: '' },
      ],
    }
    api.testProviderModelsLiveness.mockResolvedValue(response)
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接验证')
    fireEvent.change(screen.getByPlaceholderText(ST.testPromptPlaceholder), { target: { value: '请列出整理文件的三个步骤。' } })
    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testAll }))
    await screen.findByText(T.settings.testResult.ok)
    expect(screen.getByText(T.settings.testResult.auth)).toBeInTheDocument()
    expect(screen.getByText('首字 0.8 秒')).toBeInTheDocument()
    expect(screen.getByText(T.settings.usableCount(1, 2))).toBeInTheDocument()
    expect(screen.getByText(T.settings.fastest('gpt-5'))).toBeInTheDocument()

    const [input, options] = api.testProviderModelsLiveness.mock.calls[0] as [Record<string, unknown>, { signal: AbortSignal }]
    expect(input.providerId).toBe('p-main')
    expect(input.models).toEqual(['gpt-5', 'glm-5'])
    expect(input.prompt).toBe('请列出整理文件的三个步骤。')
    expect(options.signal).toBeInstanceOf(AbortSignal)
    modelPane('模型与默认值')
    expect(screen.getByText(ST.verifiedModels(1, 2))).toBeInTheDocument()
    await user.selectOptions(screen.getByLabelText(ST.resultFilter), 'verified')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeInTheDocument()
    expect(screen.queryByRole('checkbox', { name: 'glm-5' })).not.toBeInTheDocument()
  })

  it('自动时测一遍：每个模型显示挨个试出来走哪种、会不会调工具，先试过不行的也说出来', async () => {
    const user = userEvent.setup()
    api.readProviderCatalog.mockResolvedValue(catalog([profile({ wire_format_explicit: false })]))
    const response: ProviderLivenessResponse = {
      schema: 'zhimeng.provider-model-liveness.v1',
      tested: 2,
      usable_count: 2,
      usable_models: ['gpt-5', 'glm-5'],
      fastest_model: 'gpt-5',
      results: [
        {
          model: 'gpt-5', status: 'ok', usable: true, label: '', note: '', first_byte_ms: 800, elapsed_ms: 1200, reply: 'ok',
          wire_format: 'responses', auto: true, tools: 'ok', tools_note: '',
          tried: [{ wire_format: 'chat', status: 'not_found' }, { wire_format: 'responses', status: 'ok', tools: 'ok' }],
        },
        {
          model: 'glm-5', status: 'ok', usable: true, label: '', note: '', first_byte_ms: 900, elapsed_ms: 1300, reply: 'ok',
          wire_format: 'chat', auto: true, tools: 'no', tools_note: '能回话，但没有调工具',
          tried: [{ wire_format: 'chat', status: 'ok', tools: 'no' }],
        },
      ],
    }
    api.testProviderModelsLiveness.mockResolvedValue(response)
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testAll }))
    await screen.findByText(T.settings.routeAuto('Responses'))
    expect(screen.getByText(T.settings.routeAuto('Chat'))).toBeInTheDocument()
    expect(screen.getByText(T.settings.toolsOk)).toBeInTheDocument()
    expect(screen.getByText(T.settings.toolsNo)).toBeInTheDocument()
    expect(screen.getByText('能回话，但没有调工具')).toBeInTheDocument()
    expect(screen.getByText(T.settings.routeTriedBefore(`Chat（${T.settings.testResult.not_found}）`))).toBeInTheDocument()
    const [input] = api.testProviderModelsLiveness.mock.calls[0] as [Record<string, unknown>]
    expect(input.wireFormat).toBe('auto')
  })

  it('区分工具结果没接上与没有调工具，并正确显示之前的失败协议', async () => {
    const user = userEvent.setup()
    api.readProviderCatalog.mockResolvedValue(catalog([profile({ wire_format_explicit: false })]))
    api.testProviderModelsLiveness.mockResolvedValue({
      schema: 'zhimeng.provider-model-liveness.v1', tested: 2, usable_count: 2,
      usable_models: ['gpt-5', 'glm-5'], fastest_model: 'gpt-5',
      results: [
        {
          model: 'gpt-5', status: 'ok', usable: true, label: '', note: '', first_byte_ms: 800, elapsed_ms: 1200, reply: 'ok',
          wire_format: 'responses', auto: true, tools: 'ok',
          tried: [{ wire_format: 'chat', status: 'ok', tools: 'tool_roundtrip_failed' }, { wire_format: 'responses', status: 'ok', tools: 'ok' }],
        },
        {
          model: 'glm-5', status: 'ok', usable: true, label: '', note: '', first_byte_ms: 900, elapsed_ms: 1300, reply: 'ok',
          wire_format: 'chat', auto: true, tools: 'failed', tools_note: '工具结果已回传，但没有继续回答。',
          tool_call_emitted: true, tool_roundtrip: 'failed',
        },
      ],
    } satisfies ProviderLivenessResponse)
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testAll }))
    expect(await screen.findByText(T.settings.toolsRoundtripFailed)).toBeInTheDocument()
    expect(screen.getByText(T.settings.routeTriedBefore(`Chat（${T.settings.toolsRoundtripFailed}）`))).toBeInTheDocument()
    expect(screen.getByText('工具结果已回传，但没有继续回答。')).toBeInTheDocument()
    expect(screen.queryByText(T.settings.toolsNo)).not.toBeInTheDocument()
  })

  it('自动服务保存也只保存，连接测试由用户单独触发', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    const auto = profile({
      wire_format_explicit: false,
      verified_routes: { 'gpt-5': { wire_format: 'responses', tools: true, verified_at: '2026-10-05T21:30:00+08:00' } },
    })
    api.readProviderCatalog.mockResolvedValue(catalog([auto]))
    api.saveProviderProfile.mockResolvedValue(saveResult(auto, [auto]))
    api.testProviderModelsLiveness.mockResolvedValue({
      schema: 'zhimeng.provider-model-liveness.v1',
      tested: 1,
      usable_count: 1,
      usable_models: ['glm-5'],
      fastest_model: 'glm-5',
      results: [{
        model: 'glm-5', status: 'ok', usable: true, label: '', note: '', first_byte_ms: 900, elapsed_ms: 1300, reply: 'ok',
        wire_format: 'chat', auto: true, tools: 'ok', tools_note: '', tried: [{ wire_format: 'chat', status: 'ok', tools: 'ok' }],
      }],
    } satisfies ProviderLivenessResponse)
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接验证')
    fireEvent.change(screen.getByPlaceholderText(ST.testPromptPlaceholder), { target: { value: '请说明如何检查一份表格。' } })
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(props.notify).toHaveBeenCalledWith('ok', T.settings.saved))
    expect(api.testProviderModelsLiveness).not.toHaveBeenCalled()
    modelPane('连接验证')
    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testAll }))
    await waitFor(() => expect(api.testProviderModelsLiveness).toHaveBeenCalledTimes(1))
    const [input] = api.testProviderModelsLiveness.mock.calls[0] as [Record<string, unknown>]
    expect(input.models).toEqual(['gpt-5', 'glm-5'])
    expect(input.wireFormat).toBe('auto')
    expect(input.providerId).toBe('p-main')
    expect(input.prompt).toBe('请说明如何检查一份表格。')
    await screen.findByText(T.settings.routeAuto('Chat'))
    // 测完重读一次列表（打开时读一次，测完再读一次）
    await waitFor(() => expect(api.readProviderCatalog).toHaveBeenCalledTimes(2))
  })

  it('清除接口记录并重测前使用设置确认框，确认后才发请求', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.readProviderCatalog.mockResolvedValue(catalog([profile({ wire_format_explicit: false })]))
    api.clearVerifiedRoutes.mockResolvedValue({ cleared: true })
    api.testProviderModelsLiveness.mockResolvedValue({ tested: 0, usable_count: 0, fastest_model: null, results: [] })
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('高级选项')
    fireEvent.change(screen.getByLabelText(T.settings.authType), { target: { value: 'custom' } })
    modelPane('高级选项')
    fireEvent.change(screen.getByLabelText(new RegExp(`^${T.settings.extraHeaders}`)), { target: { value: 'X-Client: new-route' } })
    modelPane('连接验证')
    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.retestAll }))
    expect(api.clearVerifiedRoutes).not.toHaveBeenCalled()
    expect(api.testProviderModelsLiveness).not.toHaveBeenCalled()
    await vi.mocked(props.askConfirm).mock.calls[0][0].onConfirm()
    expect(api.clearVerifiedRoutes).toHaveBeenCalledWith({ baseUrl: 'https://relay.example/v1', apiKey: undefined, providerId: 'p-main', authType: 'custom', extraHeaders: { 'X-Client': 'new-route' } })
    expect(api.testProviderModelsLiveness).toHaveBeenCalledOnce()
  })

  it('明确选了接口的服务，保存完不自动测', async () => {
    const user = userEvent.setup()
    const chosen = profile({ wire_format_explicit: true })
    api.readProviderCatalog.mockResolvedValue(catalog([chosen]))
    api.saveProviderProfile.mockResolvedValue(saveResult(chosen, [chosen]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    await user.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledTimes(1))
    expect(api.testProviderModelsLiveness).not.toHaveBeenCalled()
  })

  it('自动的服务：上次测活测出来的协议，打开时就显示在模型旁边', async () => {
    api.readProviderCatalog.mockResolvedValue(catalog([profile({
      wire_format_explicit: false,
      verified_routes: { 'gpt-5': { wire_format: 'responses', tools: true, verified_at: '2026-10-05T21:30:00+08:00' } },
    })]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    expect(await screen.findByText(T.settings.routeSaved('Responses', true))).toBeInTheDocument()
  })

  it('测试进行中可以停止，停下来后提示已停止', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.testProviderModelsLiveness.mockImplementation((_input: unknown, options: { signal: AbortSignal }) => new Promise((_resolve, reject) => {
      options.signal.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError')))
    }))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testAll }))
    await screen.findByText(T.settings.testing)
    modelPane('连接验证')
    await user.click(screen.getByRole('button', { name: T.settings.testCancel }))

    await waitFor(() => expect(props.notify).toHaveBeenCalledWith('info', ST.testCancelled))
    modelPane('连接验证')
    expect(screen.getByRole('button', { name: T.settings.testAll })).toBeInTheDocument()
    expect(props.notify).not.toHaveBeenCalledWith('danger', expect.anything())
  })

  it.each([
    ['地址', T.settings.baseUrl, 'https://new-relay.example/v1'],
    ['密钥', T.settings.apiKey, 'new-draft-key'],
    ['认证', T.settings.authType, 'x-api-key'],
    ['请求头', new RegExp(`^${T.settings.extraHeaders}`), 'X-Client: changed'],
    ['接口', T.settings.wireFormat, 'anthropic_messages'],
    ['接受的模型名', new RegExp(`^${T.settings.acceptedNames}`), 'new-model-alias'],
  ])('修改%s后，旧连接的文字、工具与保存路线结果都不再算当前草稿的验证', async (_name, label, value) => {
    const auto = profile({ wire_format_explicit: false,
      verified_routes: { 'gpt-5': { wire_format: 'responses', tools: true, verified_at: '2026-10-09T10:00:00+08:00' } },
    })
    api.readProviderCatalog.mockResolvedValue(catalog([auto]))
    api.testProviderModelsLiveness.mockResolvedValue({
      schema: 'zhimeng.provider-model-liveness.v1', tested: 1, usable_count: 1,
      usable_models: ['gpt-5'], fastest_model: 'gpt-5', results: [{
        model: 'gpt-5', status: 'ok', usable: true, label: '', note: '',
        first_byte_ms: 500, elapsed_ms: 900, reply: 'ok', wire_format: 'responses', auto: true, tools: 'ok',
      }],
    } satisfies ProviderLivenessResponse)
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'gpt-5', reply: 'ok', latency_ms: 1234, wire_format: 'responses' })
    render(<SettingsView {...makeProps()} />)
    await screen.findByText(T.settings.routeSaved('Responses', true))
    const input = screen.getByLabelText(label) as HTMLInputElement | HTMLSelectElement | HTMLTextAreaElement
    const previous = input.value
    fireEvent.change(input, { target: { value } })
    expect(screen.queryByText(T.settings.routeSaved('Responses', true))).not.toBeInTheDocument()
    fireEvent.change(input, { target: { value: previous } })
    expect(screen.getByText(T.settings.routeSaved('Responses', true))).toBeInTheDocument()
    modelPane('连接验证')
    fireEvent.click(screen.getByRole('button', { name: T.settings.testAll }))
    await screen.findByText(T.settings.toolsOk)
    modelPane('模型与默认值')
    fireEvent.click(screen.getAllByRole('button', { name: T.settings.testModel })[0])
    await screen.findByText(ST.testOk('1.2'))

    fireEvent.change(screen.getByLabelText(label), { target: { value } })

    expect(screen.queryByText(ST.testOk('1.2'))).not.toBeInTheDocument()
    expect(screen.queryByText(T.settings.toolsOk)).not.toBeInTheDocument()
    expect(screen.queryByText(T.settings.routeAuto('Responses'))).not.toBeInTheDocument()
    expect(screen.queryByText(T.settings.routeSaved('Responses', true))).not.toBeInTheDocument()
    expect(screen.queryByText(T.settings.usableCount(1, 1))).not.toBeInTheDocument()
    expect(screen.queryByText(T.settings.fastest('gpt-5'))).not.toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '服务配置' })).getByText('未保存')).toBeInTheDocument()
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
  })

  it('改名保留同一连接的验证；文字测试说明实际接口且不冒充工具验证', async () => {
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'gpt-5', reply: 'ok', latency_ms: 1234, wire_format: 'responses' })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    fireEvent.click(screen.getAllByRole('button', { name: T.settings.testModel })[0])
    await screen.findByText(ST.testOk('1.2'))
    expect(screen.getByText('本次文字请求：Responses')).toBeInTheDocument()
    expect(screen.getByText('这里只验证文字回复；工具能力可用「验证回复与工具」检查。')).toBeInTheDocument()
    expect(screen.queryByText(T.settings.toolsOk)).not.toBeInTheDocument()
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: '新名称' } })
    modelPane('模型与默认值')
    expect(screen.getByText(ST.testOk('1.2'))).toBeInTheDocument()
    expect(within(screen.getByRole('region', { name: '服务配置' })).getByText('未保存')).toBeInTheDocument()
  })

  it('测试新连接草稿不自动保存；保存后清除临时结果并使用已保存线路的记录', async () => {
    const saved = profile({ base_url: 'https://changed.example/v1', wire_format: 'auto', wire_format_explicit: false,
      verified_routes: { 'gpt-5': { wire_format: 'responses', tools: true, verified_at: '2026-10-09T10:00:00+08:00' } },
    })
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'gpt-5', reply: 'ok', latency_ms: 1234, wire_format: 'responses' })
    api.saveProviderProfile.mockResolvedValue(saveResult(saved, [saved]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.baseUrl), { target: { value: saved.base_url } })
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.wireFormat), { target: { value: 'auto' } })
    modelPane('模型与默认值')
    fireEvent.click(screen.getAllByRole('button', { name: T.settings.testModel })[0])
    await screen.findByText(ST.testOk('1.2'))
    expect(screen.getByText(ST.testedDraft)).toBeInTheDocument()
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await screen.findByText(T.settings.routeSaved('Responses', true))
    expect(screen.queryByText(ST.testOk('1.2'))).not.toBeInTheDocument()
    expect(screen.queryByText(ST.testedDraft)).not.toBeInTheDocument()
  })

  it('工具验证收到空正文时分别显示正文待核验和工具成功', async () => {
    api.testProviderModelsLiveness.mockResolvedValue({ tested: 1, usable_count: 1, fastest_model: 'gpt-5', results: [{
      model: 'gpt-5', status: 'ok', usable: true, reply: '', note: 'Only reasoning returned.', tools: 'ok', wire_format: 'responses',
    }] })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接验证')
    fireEvent.click(screen.getByRole('button', { name: T.settings.testAll }))
    await screen.findByText('尚未收到正文')
    expect(screen.getByText(T.settings.toolsOk)).toBeInTheDocument()
    expect(screen.queryByText(T.settings.testResult.ok)).not.toBeInTheDocument()
  })

  it('单模型连接能回显、保存、继承并保留未选模型', async () => {
    const initial = profile({ model_overrides: { 'hidden-model': { overridden_fields: ['base_url', 'api_key'],
      base_url: 'https://specific.example/v1', api_key_set: true, extra_header_names: ['X-Account'] } } })
    api.readProviderCatalog.mockResolvedValue(catalog([initial]))
    api.patchProviderModelOverride.mockResolvedValue(catalog([initial]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    fireEvent.click(await screen.findByRole('button', { name: 'hidden-model 的连接设置' }))
    expect(screen.getByLabelText('模型地址')).toHaveValue('https://specific.example/v1')
    expect(screen.getByLabelText('模型密钥')).toHaveValue('')
    fireEvent.change(screen.getByLabelText('接口协议'), { target: { value: 'responses' } })
    fireEvent.change(screen.getByLabelText('模型地址'), { target: { value: '' } })
    fireEvent.click(screen.getByLabelText('恢复使用服务密钥'))
    fireEvent.click(screen.getByRole('button', { name: '保存例外设置' }))
    await waitFor(() => expect(api.patchProviderModelOverride).toHaveBeenCalledWith('p-main', 'hidden-model', expect.objectContaining({
      base_url: null, wire_format: 'responses', inherit_api_key: true, extra_headers: {}, remove_header_names: [],
    })))
    await waitFor(() => expect(screen.queryByRole('dialog')).not.toBeInTheDocument())
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'hidden-model' })).not.toBeChecked()
  })

  it('单模型连接保存失败保留编辑内容，可重试或恢复服务配置', async () => {
    const props = makeProps()
    const initial = profile({ model_overrides: { 'gpt-5': { overridden_fields: ['wire_format'], wire_format: 'responses', api_key_set: false, extra_header_names: [] } } })
    api.readProviderCatalog.mockResolvedValue(catalog([initial]))
    api.patchProviderModelOverride.mockRejectedValueOnce(new Error('Failed to save connection')).mockResolvedValue(catalog([profile()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    fireEvent.click(await screen.findByRole('button', { name: 'gpt-5 的连接设置' }))
    fireEvent.change(screen.getByLabelText('模型地址'), { target: { value: 'https://draft.example/v1' } })
    fireEvent.click(screen.getByRole('button', { name: '保存例外设置' }))
    await screen.findByText('Failed to save connection')
    expect(screen.getByLabelText('模型地址')).toHaveValue('https://draft.example/v1')
    fireEvent.click(screen.getByRole('button', { name: '全部沿用服务' }))
    expect(api.patchProviderModelOverride).toHaveBeenCalledTimes(1)
    await act(async () => { await vi.mocked(props.askConfirm).mock.calls.at(-1)![0].onConfirm() })
    await waitFor(() => expect(api.patchProviderModelOverride).toHaveBeenLastCalledWith('p-main', 'gpt-5', { reset: true }))
  })

  it('单模型例外关闭会保护未保存草稿', async () => {
    const props = makeProps()
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    fireEvent.click(await screen.findByRole('button', { name: 'gpt-5 的连接设置' }))
    fireEvent.change(screen.getByLabelText('接口协议'), { target: { value: 'responses' } })
    fireEvent.click(screen.getByRole('button', { name: '取消' }))
    await waitFor(() => expect(props.askConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '此模型的例外设置尚未保存' })))
    expect(screen.getByLabelText('接口协议')).toHaveValue('responses')
    const request = vi.mocked(props.askConfirm).mock.calls.at(-1)![0]
    await act(async () => { await request.onConfirm() })
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it.each(['', undefined])('探测连通但正文为 %s 时不声称文字已返回', async (reply) => {
    api.testProviderModel.mockResolvedValue({ ok: true, model: 'gpt-5', reply, latency_ms: 1234, wire_format: 'responses' })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    fireEvent.click(screen.getAllByRole('button', { name: T.settings.testModel })[0])
    await screen.findByText('尚未收到正文 · 1.2 秒')
    expect(screen.queryByText(ST.testOk('1.2'))).not.toBeInTheDocument()
    expect(screen.getByText('本次未收到正文，回复能力仍需进一步核验。')).toBeInTheDocument()
  })

  it('测一下单个模型：能用显示用时，没测通显示原因', async () => {
    const user = userEvent.setup()
    api.testProviderModel
      .mockResolvedValueOnce({ schema: 'zhimeng.provider-test.v1', ok: true, model: 'gpt-5', reply: '连接成功', latency_ms: 1234 })
      .mockRejectedValueOnce(new Error('模型返回了空内容'))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })

    modelPane('模型与默认值')
    const buttons = screen.getAllByRole('button', { name: T.settings.testModel })
    expect(buttons).toHaveLength(2)
    await user.click(buttons[0])
    await screen.findByText(ST.testOk('1.2'))
    const input = api.testProviderModel.mock.calls[0][0] as Record<string, unknown>
    expect(input.model).toBe('gpt-5')
    expect(input.prompt).toBe(ST.testPrompt)
    expect(input.providerId).toBe('p-main')

    modelPane('连接验证')
    fireEvent.change(screen.getByPlaceholderText(ST.testPromptPlaceholder), { target: { value: '请说明如何整理目录。' } })
    modelPane('模型与默认值')
    await user.click(screen.getAllByRole('button', { name: T.settings.testModel })[1])
    await screen.findByText('模型返回了空内容')
    expect(api.testProviderModel.mock.calls[1][0].prompt).toBe('请说明如何整理目录。')
  })

  it('「用这套」切到另一套：切完重读列表、刷新当前模型、提示已保存', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.activateProviderProfile.mockResolvedValue({})
    api.readProviderCatalog
      .mockResolvedValueOnce(catalog([profile(), spare()]))
      .mockResolvedValueOnce(catalog([profile({ active: false }), spare()].map((item) => item.provider_id === 'p-spare' ? { ...item, active: true } : item)))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    await user.click(screen.getByRole('button', { name: /^备用/ }))
    await user.click(screen.getByRole('button', { name: T.settings.useThis }))
    await waitFor(() => expect(api.activateProviderProfile).toHaveBeenCalledWith('p-spare', {}, undefined, undefined))
    await waitFor(() => expect(props.onProviderChanged).toHaveBeenCalledTimes(1))
    expect(props.notify).toHaveBeenCalledWith('ok', ST.activated)
    await waitFor(() => expect(within(serviceItem('备用')).getByText(T.settings.inUse)).toBeInTheDocument())
    await user.click(screen.getByRole('button', { name: /^主力/ }))
    expect(screen.getByRole('button', { name: T.settings.useThis })).toBeInTheDocument()
  })

  it('删除这套要先确认；确认后从列表里消失并提示', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.deleteProviderProfile.mockResolvedValue(catalog([profile()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })

    await user.click(screen.getByRole('button', { name: /^备用/ }))
    await user.click(screen.getByRole('button', { name: T.settings.deleteService }))
    expect(api.deleteProviderProfile).not.toHaveBeenCalled()
    expect(props.askConfirm).toHaveBeenCalledTimes(1)
    const request = (props.askConfirm as ReturnType<typeof vi.fn>).mock.calls[0][0] as { title: string; body: string; danger?: boolean; onConfirm: () => Promise<void> }
    expect(request.title).toBe(T.settings.deleteServiceConfirm('备用'))
    expect(request.body).toBe(T.settings.deleteServiceBody)
    expect(request.danger).toBe(true)

    await request.onConfirm()
    expect(api.deleteProviderProfile).toHaveBeenCalledWith('p-spare', {}, undefined)
    await waitFor(() => expect(screen.queryByRole('button', { name: /^备用/ })).toBeNull())
    expect(props.notify).toHaveBeenCalledWith('ok', ST.deleted)
    // 非默认服务可能被旧对话使用，同样刷新目录；刷新不会重新绑定会话。
    expect(props.onProviderChanged).toHaveBeenCalledOnce()
  })

  it('已保存请求头能标记移除和撤销，草稿测试与保存使用同一份移除列表', async () => {
    const initial = profile({ extra_header_names: ['X-Account', 'User-Agent'] })
    api.readProviderCatalog.mockResolvedValue(catalog([initial]))
    const saved = profile({ extra_header_names: ['X-Account'] })
    api.saveProviderProfile.mockResolvedValue(saveResult(saved, [saved]))
    api.testProviderModel.mockResolvedValue({ ok: true, reply: 'Header draft checked.', latency_ms: 20 })
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('高级选项')
    modelPane('高级选项')
    fireEvent.click(screen.getByRole('button', { name: '移除 User-Agent' }))
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
    expect(screen.getByText('保存后移除')).toBeInTheDocument()
    modelPane('高级选项')
    fireEvent.click(screen.getByRole('button', { name: '撤销移除 User-Agent' }))
    expect(screen.queryByText('保存后移除')).not.toBeInTheDocument()
    modelPane('高级选项')
    fireEvent.click(screen.getByRole('button', { name: '移除 User-Agent' }))
    modelPane('模型与默认值')
    fireEvent.click(screen.getAllByRole('button', { name: T.settings.testModel })[0])
    await waitFor(() => expect(api.testProviderModel).toHaveBeenCalledWith(expect.objectContaining({ removeHeaderNames: ['User-Agent'] })))
    await screen.findByText(ST.testOk('0.0'))
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await waitFor(() => expect(api.saveProviderProfile).toHaveBeenCalledWith(expect.objectContaining({ removeHeaderNames: ['User-Agent'] })))
    await waitFor(() => expect(screen.queryByRole('button', { name: '撤销移除 User-Agent' })).not.toBeInTheDocument())
    modelPane('高级选项')
    expect(screen.getByRole('button', { name: '移除 X-Account' })).toBeInTheDocument()
  })

  it('本机服务没连上：顶部提示，表单和按钮都禁用，也不去读列表', async () => {
    render(<SettingsView {...makeProps({ runtime: { connection: 'offline', ready: false, serviceStatus: 'offline', runStatus: null, version: null, detail: '', checkedAt: '' } })} />)

    expect(screen.getByRole('alert')).toHaveTextContent(T.runtime.offline)
    expect(screen.getByRole('button', { name: T.settings.addService })).toBeDisabled()
    expect(api.readProviderCatalog).not.toHaveBeenCalled()
  })

  it('模型信息读取失败不冒充本机离线，仍可编辑并重试', async () => {
    const reload = vi.fn(async () => undefined)
    render(<SettingsView {...makeProps({ provider: { settings: null, catalog: null, state: 'error', reload } })} />)
    await screen.findByRole('button', { name: /^主力/ })
    expect(screen.queryByText(T.runtime.offline)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: T.settings.addService })).toBeEnabled()
    fireEvent.click(screen.getByRole('button', { name: T.common.retry }))
    expect(reload).toHaveBeenCalledOnce()
  })

  it('测活触发的旧列表晚到时不能覆盖随后保存的新配置', async () => {
    let finish!: (value: ProviderCatalogView) => void
    api.readProviderCatalog.mockResolvedValueOnce(catalog([profile()]))
      .mockReturnValueOnce(new Promise(resolve => { finish = resolve }))
    api.testProviderModelsLiveness.mockResolvedValue({ results: [], usable_count: 0, tested: 0, fastest_model: null })
    const saved = profile({ name: 'Updated service' })
    api.saveProviderProfile.mockResolvedValue(saveResult(saved, [saved]))
    render(<SettingsView {...makeProps()} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接验证')
    fireEvent.click(screen.getByRole('button', { name: T.settings.testAll }))
    await waitFor(() => expect(api.readProviderCatalog).toHaveBeenCalledTimes(2))
    modelPane('连接')
    await waitFor(() => expect(screen.getByLabelText(T.settings.name)).toBeEnabled())
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: saved.name } })
    fireEvent.click(screen.getByRole('button', { name: T.settings.saveService }))
    await screen.findByRole('heading', { name: saved.name })
    await act(async () => finish(catalog([profile()])))
    expect(screen.getByRole('heading', { name: saved.name })).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /^主力/ })).not.toBeInTheDocument()
  })

  it('列表读不出来时给一条提示和「重试」', async () => {
    const user = userEvent.setup()
    api.readProviderCatalog.mockRejectedValueOnce(new Error('boom')).mockResolvedValueOnce(catalog([profile()]))
    render(<SettingsView {...makeProps()} />)

    await screen.findByText('boom')
    await user.click(screen.getByRole('button', { name: T.common.retry }))
    await screen.findByRole('button', { name: /^主力/ })
  })
})

describe('设置页 · 外壳、外观、记忆里的项目说明、关于', () => {
  beforeEach(() => {
    for (const fn of Object.values(api)) fn.mockReset()
    api.readProviderCatalog.mockResolvedValue(catalog([profile()]))
    window.localStorage.clear()
  })

  it('左边分组目录：点一节交给外面切过去，右上角关掉', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    render(<SettingsView {...props} />)
    expect(screen.getByRole('button', { name: S.nav.models })).toHaveAttribute('aria-current', 'page')
    expect(screen.getByRole('heading', { level: 1, name: S.nav.models })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: S.nav.skills }))
    expect(props.onSection).toHaveBeenCalledWith('skills')
    await user.click(screen.getByRole('button', { name: S.close }))
    expect(props.onClose).toHaveBeenCalledOnce()
    // 以前那几页（本地数据、可信文件夹、写作偏好）没有了
    expect(screen.queryByText(/本地数据|可信文件夹|写作偏好/)).toBeNull()
  })

  it('外观：主题和字号；工作方式：保存、发送、思考和工具合并', async () => {
    const user = userEvent.setup()
    const props = makeProps({ section: 'appearance' })
    const view = render(<SettingsView {...props} />)
    expect(screen.getByRole('heading', { name: '通用' })).toBeInTheDocument()

    const theme = screen.getByRole('group', { name: T.settings.theme })
    expect(within(theme).getByRole('button', { name: S.themes.light })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(theme).getByRole('button', { name: S.themes.dark }))
    expect(props.onTheme).toHaveBeenCalledWith('dark')
    await user.click(within(theme).getByRole('button', { name: S.themes.system }))
    expect(props.onTheme).toHaveBeenCalledWith('system')

    const size = screen.getByRole('group', { name: S.uiSize })
    expect(within(size).getByRole('button', { name: S.uiSizes.l })).toHaveAttribute('aria-pressed', 'true')
    await user.click(within(size).getByRole('button', { name: S.uiSizes.m }))
    expect(window.localStorage.getItem('zhimeng.app.ui-size')).toBe('m')
    expect(within(size).getByRole('button', { name: S.uiSizes.m })).toHaveAttribute('aria-pressed', 'true')

    view.rerender(<SettingsView {...props} section="behavior" />)
    expect(screen.getByText(ST.savePolicyNote)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: ST.savePolicyReview }))
    expect(window.localStorage.getItem('zhimeng.apply-policy.v1')).toBe('review')
    await user.click(screen.getByRole('switch', { name: S.behavior.thinking }))
    await user.click(screen.getByRole('switch', { name: S.behavior.groupTools }))
    expect(JSON.parse(window.localStorage.getItem('zhimeng.app.chat-preferences')!)).toEqual({ expandThinking: false, groupTools: false })
    await user.click(screen.getByRole('button', { name: '恢复默认工作方式' }))
    expect(JSON.parse(window.localStorage.getItem('zhimeng.app.chat-preferences')!)).toEqual({ expandThinking: true, groupTools: true })
    expect(window.localStorage.getItem('zhimeng.apply-policy.v1')).toBe('auto')
    view.rerender(<SettingsView {...props} section="appearance" />)
    await user.click(screen.getByRole('button', { name: '恢复默认外观' }))
    expect(window.localStorage.getItem('zhimeng.app.ui-size')).toBe('l')
    expect(props.onTheme).toHaveBeenLastCalledWith('system')
  })

  it('设置搜索可以用子功能找到对应分组', async () => {
    const props = makeProps({ section: 'about' })
    render(<SettingsView {...props} />)
    const user = userEvent.setup()
    await user.type(screen.getByRole('textbox', { name: S.search }), '恢复')
    expect(screen.getByRole('heading', { name: '搜索结果' })).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: /记忆修改历史/ }))
    expect(props.onSection).toHaveBeenCalledWith('memory')
  })

  it('记忆那一节：打开的文件夹里有 AGENTS.md 就列出来，点了打开它', async () => {
    const user = userEvent.setup()
    memoryApi.readUserMemory.mockResolvedValue({ schema: 'zhimeng.user-memory.v1', path: 'C:/m.md', exists: true, content: '', sha256: 'a', enabled: true, project_memory_file: 'AGENTS.md' })
    const snapshot = makeSnapshot({ files: [...makeSnapshot().files, makeFile('AGENTS.md')] })
    const props = makeProps({ section: 'project', projectSnapshot: snapshot })
    render(<SettingsView {...props} />)
    expect(screen.getByText(S.projectInstructions(snapshot.project.display_name))).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '打开 AGENTS.md' }))
    expect(props.onOpenProjectFile).toHaveBeenCalledWith('AGENTS.md')
  })

  it('记忆那一节：文件夹里还没有说明文件就说怎么来；没打开文件夹就不出这张卡', async () => {
    memoryApi.readUserMemory.mockResolvedValue({ schema: 'zhimeng.user-memory.v1', path: 'C:/m.md', exists: true, content: '', sha256: 'a', enabled: true, project_memory_file: 'AGENTS.md' })
    const { unmount } = render(<SettingsView {...makeProps({ section: 'project', projectSnapshot: makeSnapshot() })} />)
    expect(screen.getByText(S.projectInstructionsNone)).toBeInTheDocument()
    unmount()
    render(<SettingsView {...makeProps({ section: 'project', projectSnapshot: null })} />)
    expect(screen.getByText('当前未打开项目')).toBeInTheDocument()
    expect(screen.queryByText(S.projectInstructionsNone)).toBeNull()
  })

  it('关于：本机服务连没连上、版本；没有版本号就写「暂无」', () => {
    const { unmount } = render(<SettingsView {...makeProps({ section: 'about' })} />)
    expect(screen.getByText(T.settings.aboutIntro)).toBeInTheDocument()
    expect(screen.getByText(S.connection.online)).toBeInTheDocument()
    expect(screen.getByText('0.9.3')).toBeInTheDocument()
    unmount()

    render(<SettingsView {...makeProps({ section: 'about', runtime: { connection: 'degraded', ready: true, serviceStatus: 'ready', runStatus: null, version: null, detail: '', checkedAt: '' } })} />)
    expect(screen.getByText(S.connection.degraded)).toBeInTheDocument()
    expect(screen.getByText(T.common.none)).toBeInTheDocument()
  })

  it('检查本机连接后刷新状态，复制诊断只包含非敏感状态字段', async () => {
    const user = userEvent.setup()
    const props = makeProps({ section: 'about' })
    runtimeApi.fetchRuntimeSnapshot.mockResolvedValue({ ...props.runtime, connection: 'offline', ready: false, version: '0.9.4', detail: 'PRIVATE_DIAGNOSTIC_DETAIL', checkedAt: '2026-10-07T10:00:00Z' })
    const write = vi.spyOn(navigator.clipboard, 'writeText').mockResolvedValue(undefined)
    render(<SettingsView {...props} />)
    await user.click(screen.getByRole('button', { name: '检查本机连接' }))
    await screen.findByText(S.connection.offline)
    expect(screen.getByText('0.9.4')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '复制诊断信息' }))
    expect(JSON.parse(write.mock.calls[0][0])).toEqual({ app: '织梦', version: '0.9.4', connection: 'offline', ready: false, checkedAt: '2026-10-07T10:00:00Z' })
    expect(props.notify).toHaveBeenCalledWith('ok', '已复制本机状态')
    write.mockRestore()
  })

  it('切设置分类保留连接草稿，离开设置时提示，确认后才关闭', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    const view = render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: '还没有保存的线路' } })
    view.rerender(<SettingsView {...props} section="appearance" />)
    view.rerender(<SettingsView {...props} section="models" />)
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('还没有保存的线路')
    await user.click(screen.getByRole('button', { name: S.close }))
    expect(props.onClose).not.toHaveBeenCalled()
    const request = vi.mocked(props.askConfirm).mock.calls[0][0]
    expect(request.title).toBe('还有未保存的修改')
    await request.onConfirm()
    expect(props.onClose).toHaveBeenCalledOnce()
  })

  it('搜索定位并展开高级选项；空结果和清除搜索可用', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    await user.type(screen.getByRole('textbox', { name: S.search }), 'headers')
    await user.click(screen.getByRole('button', { name: /接口与高级连接选项/ }))
    expect(screen.getByRole('button', { name: '更多设置' })).toHaveAttribute('aria-expanded', 'true')
    modelPane('高级选项')
    expect(screen.getByLabelText(T.settings.authType)).toBeVisible()
    expect(screen.getByRole('textbox', { name: S.search })).toHaveValue('')
    await user.type(screen.getByRole('textbox', { name: S.search }), 'nothing-matches')
    expect(screen.getByText(S.noResults)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '清除搜索' }))
    expect(screen.getByRole('heading', { name: S.nav.models })).toBeInTheDocument()
  })

  it('预设只填新连接，替换草稿确认后清除密钥，不自动保存或测试', async () => {
    const props = makeProps()
    api.readProviderCatalog.mockResolvedValue(catalog([]))
    render(<SettingsView {...props} />)
    await screen.findByRole('group', { name: '连接预设' })
    fireEvent.click(screen.getByRole('button', { name: 'DeepSeek' }))
    expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://api.deepseek.com')
    fireEvent.change(screen.getByLabelText(T.settings.apiKey), { target: { value: 'test-draft-secret' } })
    fireEvent.click(screen.getByRole('button', { name: 'Anthropic' }))
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('test-draft-secret')
    await act(async () => { await vi.mocked(props.askConfirm).mock.calls[0][0].onConfirm() })
    expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://api.anthropic.com')
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('')
    expect(api.saveProviderProfile).not.toHaveBeenCalled()
    expect(api.testProviderModel).not.toHaveBeenCalled()
  })

  it('两项连接和新连接的草稿分别保留，离开提示列出资源', async () => {
    const props = makeProps()
    api.readProviderCatalog.mockResolvedValue(catalog([profile(), spare()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    fireEvent.change(screen.getByLabelText(T.settings.apiKey), { target: { value: 'main-draft' } })
    fireEvent.click(screen.getByRole('button', { name: /^备用/ }))
    fireEvent.change(screen.getByLabelText(T.settings.apiKey), { target: { value: 'spare-draft' } })
    fireEvent.click(screen.getByRole('button', { name: /^主力/ }))
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('main-draft')
    fireEvent.click(screen.getByRole('button', { name: /^备用/ }))
    expect(screen.getByLabelText(T.settings.apiKey)).toHaveValue('spare-draft')
    fireEvent.click(screen.getByRole('button', { name: T.settings.addService }))
    fireEvent.change(screen.getByLabelText(T.settings.baseUrl), { target: { value: 'https://draft.example' } })
    fireEvent.click(screen.getByRole('button', { name: /^主力/ }))
    fireEvent.click(screen.getByRole('button', { name: new RegExp(T.settings.addService) }))
    expect(screen.getByLabelText(T.settings.baseUrl)).toHaveValue('https://draft.example')
    fireEvent.click(screen.getByRole('button', { name: S.close }))
    expect(props.askConfirm).toHaveBeenCalledWith(expect.objectContaining({ body: expect.stringMatching(/主力.*备用|备用.*主力/) }))
    expect(props.onClose).not.toHaveBeenCalled()
  })

  it('筛选不更改勾选，切换服务独立保留草稿', async () => {
    const user = userEvent.setup()
    const props = makeProps()
    api.readProviderCatalog.mockResolvedValue(catalog([profile(), spare()]))
    render(<SettingsView {...props} />)
    await screen.findByRole('button', { name: /^主力/ })
    modelPane('模型与默认值')
    await user.type(screen.getByRole('textbox', { name: '搜索模型' }), 'glm')
    expect(screen.queryByRole('checkbox', { name: 'gpt-5' })).toBeNull()
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'glm-5' })).toBeChecked()
    modelPane('模型与默认值')
    await user.clear(screen.getByRole('textbox', { name: '搜索模型' }))
    modelPane('模型与默认值')
    expect(screen.getByRole('checkbox', { name: 'gpt-5' })).toBeChecked()
    modelPane('连接')
    fireEvent.change(screen.getByLabelText(T.settings.name), { target: { value: '草稿' } })
    await user.click(screen.getByRole('button', { name: /^备用/ }))
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('备用')
    expect(props.askConfirm).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: /^主力/ }))
    modelPane('连接')
    expect(screen.getByLabelText(T.settings.name)).toHaveValue('草稿')
  })
})

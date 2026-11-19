import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { useProviderSettings } from './useProviderSettings'
import { publishSettingsChange } from '../../lib/settingsChanges'

const api = vi.hoisted(() => ({ settings: vi.fn(), catalog: vi.fn() }))
vi.mock('../../api/providerSettings', () => ({ readProviderSettings: api.settings, readProviderCatalog: api.catalog }))
afterEach(cleanup)
beforeEach(() => {
  vi.resetAllMocks()
  api.settings.mockResolvedValue({ provider_id: 'active', model: 'global-model' })
  api.catalog.mockResolvedValue({ providers: [{ provider_id: 'old', model: 'old-model', context_window: '256k' }] })
})

it('读取全局默认及各服务配置，供旧会话准确显示自身模型和上下文', async () => {
  const { result } = renderHook(() => useProviderSettings(true))
  await waitFor(() => expect(result.current.state).toBe('ready'))
  expect(result.current.settings?.model).toBe('global-model')
  expect(result.current.catalog?.providers[0].provider_id).toBe('old')
})

it('优先使用同次目录快照的默认值，变更通知刷新消费者而不改绑定', async () => {
  api.catalog.mockResolvedValue({ revision: 'first', providers: [{ provider_id: 'old', model: 'old-model' }],
    active_settings: { provider_id: 'old', model: 'old-model', revision: 'first' } })
  const { result } = renderHook(() => useProviderSettings(true))
  await waitFor(() => expect(result.current.state).toBe('ready'))
  expect(result.current.settings?.model).toBe('old-model')
  expect(api.settings).not.toHaveBeenCalled()
  api.catalog.mockResolvedValue({ revision: 'next', providers: [{ provider_id: 'new', model: 'new-model' }],
    active_settings: { provider_id: 'new', model: 'new-model', revision: 'next' } })
  await act(async () => publishSettingsChange('providers'))
  await waitFor(() => expect(result.current.settings?.model).toBe('new-model'))
  expect(result.current.catalog?.revision).toBe(result.current.settings?.revision)
  expect(api.settings).not.toHaveBeenCalled()
})

it('后台初次读取失败记录状态；主动保存后刷新失败向调用者抛错', async () => {
  api.settings.mockRejectedValue(new Error('模型设置读取失败'))
  const { result } = renderHook(() => useProviderSettings(true))
  await waitFor(() => expect(result.current.state).toBe('error'))
  await act(async () => { await expect(result.current.reload({ throwOnError: true })).rejects.toThrow('模型设置读取失败') })
  expect(result.current.state).toBe('error')
})

it.each([false, true])('新配置加载后旧请求的结果或错误不倒退当前状态（旧请求失败=%s）', async (failOld) => {
  let finish!: (value: unknown) => void
  let fail!: (error: Error) => void
  api.settings.mockReturnValueOnce(new Promise((resolve, reject) => { finish = resolve; fail = reject }))
  const { result } = renderHook(() => useProviderSettings(true))
  await waitFor(() => expect(api.settings).toHaveBeenCalledOnce())
  await act(async () => { await result.current.reload() })
  expect(result.current.settings?.model).toBe('global-model')
  await act(async () => {
    if (failOld) fail(new Error('Older request failed'))
    else finish({ provider_id: 'obsolete', model: 'obsolete-model' })
  })
  expect(result.current.settings?.model).toBe('global-model')
  expect(result.current.state).toBe('ready')
})

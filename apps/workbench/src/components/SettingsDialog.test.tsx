import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { SettingsDialog } from './SettingsDialog'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const emptySettings = {
  schema: 'zhimeng.provider-settings.v1',
  base_url: '',
  model: '',
  api_key_set: false,
  source: 'none',
  configured: false,
}

describe('SettingsDialog', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('saves a manually entered model without requiring model discovery', async () => {
    const user = userEvent.setup()
    const onSaved = vi.fn()
    let savedBody: Record<string, unknown> | null = null

    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input, init) => {
      const url = String(input)
      if (url.endsWith('/api/v1/settings/provider') && init?.method === 'PUT') {
        savedBody = JSON.parse(String(init.body)) as Record<string, unknown>
        return jsonResponse({
          ...emptySettings,
          base_url: 'https://provider.example/v1',
          model: 'author-model',
          api_key_set: true,
          source: 'settings',
          configured: true,
        })
      }
      if (url.endsWith('/api/v1/settings/provider')) return jsonResponse(emptySettings)
      throw new Error(`Unexpected URL: ${url}`)
    }))

    render(<SettingsDialog open onClose={vi.fn()} onSaved={onSaved} />)

    const baseUrl = await screen.findByLabelText('接口地址')
    await waitFor(() => expect(baseUrl).toBeEnabled())
    await user.type(baseUrl, 'https://provider.example/v1')
    await user.type(screen.getByLabelText('密钥'), 'test-key')
    await user.type(screen.getByLabelText('模型'), 'author-model')
    await user.click(screen.getByRole('button', { name: '保存' }))

    await waitFor(() => expect(onSaved).toHaveBeenCalledTimes(1))
    expect(savedBody).toEqual({
      base_url: 'https://provider.example/v1',
      model: 'author-model',
      api_key: 'test-key',
    })
  })

  it('keeps the author-entered model when discovery returns different suggestions', async () => {
    const user = userEvent.setup()

    vi.stubGlobal('fetch', vi.fn<typeof fetch>(async (input) => {
      const url = String(input)
      if (url.endsWith('/api/v1/settings/provider/models')) {
        return jsonResponse({ models: ['listed-model-a', 'listed-model-b'] })
      }
      if (url.endsWith('/api/v1/settings/provider')) return jsonResponse(emptySettings)
      throw new Error(`Unexpected URL: ${url}`)
    }))

    render(<SettingsDialog open onClose={vi.fn()} />)

    const baseUrl = await screen.findByLabelText('接口地址')
    await waitFor(() => expect(baseUrl).toBeEnabled())
    await user.type(baseUrl, 'https://provider.example/v1')
    const model = screen.getByLabelText('模型')
    await user.type(model, 'author-model')
    await user.click(screen.getByRole('button', { name: '获取模型' }))

    expect(await screen.findByText(/可以从建议中选择，也可以继续手填/)).toBeInTheDocument()
    expect(model).toHaveValue('author-model')
  })
})

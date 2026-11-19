import { expect, it, vi } from 'vitest'
import { activateProviderProfile, deleteProviderProfile, saveProviderProfile, testProviderModel, testProviderModelsLiveness } from './providerSettings'
import { createMcpServer, deleteMcpServer, reconnectMcpServer, setMcpServerEnabled, updateMcpServer } from './mcp'

it('settings tests explicitly serialize the current service draft scope', async () => {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response(JSON.stringify({
    schema: 'zhimeng.provider-test.v1', ok: true, model: 'plain', reply: 'ok', latency_ms: 1, wire_format: 'chat',
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  await testProviderModel({ providerId: 'relay', baseUrl: 'http://127.0.0.1:8788/v1', model: 'plain', prompt: 'Check', wireFormat: 'chat' }, { fetcher })
  const [, textInit] = fetcher.mock.calls[0]
  expect(JSON.parse(String(textInit?.body))).toMatchObject({ test_scope: 'service_draft', model: 'plain' })

  fetcher.mockResolvedValue(new Response(JSON.stringify({
    schema: 'zhimeng.provider-model-liveness.v1', tested: 0, usable_count: 0, usable_models: [], fastest_model: null, results: [],
  }), { status: 200, headers: { 'Content-Type': 'application/json' } }))
  await testProviderModelsLiveness({ providerId: 'relay', baseUrl: 'http://127.0.0.1:8788/v1', models: ['plain'], wireFormat: 'auto' }, { fetcher })
  const [, toolsInit] = fetcher.mock.calls[1]
  expect(JSON.parse(String(toolsInit?.body))).toMatchObject({ test_scope: 'service_draft', models: ['plain'], wire_format: 'auto' })
})

it('sends the displayed revision on provider and MCP mutations, including create retry identities', async () => {
  const fetcher = vi.fn<typeof fetch>().mockImplementation(async () => new Response('{}', {
    status: 200, headers: { 'Content-Type': 'application/json' },
  }))
  const options = { fetcher }
  const revision = 'a'.repeat(64)
  const catalogRevision = 'b'.repeat(64)
  await saveProviderProfile({ providerId: 'provider-draft', name: 'draft', selectedModels: ['plain'],
    reasoningEffort: 'auto', wireFormat: 'auto', acceptedReportedModels: [], makeActive: false,
    expectedRevision: 'missing' }, options)
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toMatchObject({
    provider_id: 'provider-draft', expected_revision: 'missing', make_active: false,
  })
  await activateProviderProfile('id/with space', options, revision, catalogRevision)
  let url = new URL(String(fetcher.mock.calls.at(-1)?.[0]), 'http://localhost')
  expect(url.pathname).toContain('/id%2Fwith%20space/activate')
  expect(url.searchParams.get('expected_revision')).toBe(revision)
  expect(url.searchParams.get('expected_catalog_revision')).toBe(catalogRevision)
  await deleteProviderProfile('provider-draft', options, revision)
  url = new URL(String(fetcher.mock.calls.at(-1)?.[0]), 'http://localhost')
  expect(url.searchParams.get('expected_revision')).toBe(revision)

  const body = { creation_id: `mcp-${'c'.repeat(32)}`, expected_revision: 'missing', name: 'draft',
    command: 'fixture', args: [], cwd: null, enabled: false, env: {}, remove_env: [] }
  await createMcpServer(body, options)
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual(body)
  await updateMcpServer(body.creation_id, { ...body, expected_revision: revision }, options)
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body)).expected_revision).toBe(revision)
  await setMcpServerEnabled(body.creation_id, true, options, revision)
  expect(JSON.parse(String(fetcher.mock.calls.at(-1)?.[1]?.body))).toEqual({ enabled: true, expected_revision: revision })
  await reconnectMcpServer(body.creation_id, options, revision)
  expect(new URL(String(fetcher.mock.calls.at(-1)?.[0]), 'http://localhost').searchParams.get('expected_revision')).toBe(revision)
  await deleteMcpServer(body.creation_id, options, revision)
  expect(new URL(String(fetcher.mock.calls.at(-1)?.[0]), 'http://localhost').searchParams.get('expected_revision')).toBe(revision)
})

import { beforeEach, describe, expect, it, vi } from 'vitest'
import { evaluateGatePipeline, getGatePipeline } from './gatePipeline'

const fetchRuntimeJson = vi.fn()

vi.mock('./runtime', () => ({
  fetchRuntimeJson: (...args: unknown[]) => fetchRuntimeJson(...args),
}))

describe('gatePipeline client', () => {
  beforeEach(() => {
    fetchRuntimeJson.mockReset()
  })

  it('gets and evaluates gate pipeline endpoints', async () => {
    fetchRuntimeJson.mockResolvedValue({
      schema: 'zhimeng.gate-pipeline-response.v1',
      change_set_id: 'changeset-1',
      receipt: null,
      history: [],
      not_claiming: ['literary_quality_score'],
    })

    await getGatePipeline('changeset-1')
    expect(fetchRuntimeJson).toHaveBeenCalledWith(
      '/api/v1/change-sets/changeset-1/gate-pipeline',
      {},
    )

    await evaluateGatePipeline('changeset-1', { expected_review_revision: 3 })
    expect(fetchRuntimeJson.mock.calls[1]?.[0]).toContain('/gate-pipeline/evaluate')
    expect(fetchRuntimeJson.mock.calls[1]?.[2]).toMatchObject({ method: 'POST' })
  })
})

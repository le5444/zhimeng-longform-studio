import { cleanup, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentCandidateOutput } from '../api/agent'
import { writerTraceFixture } from '../test/writerTraceFixture'
import { useWriterTrace } from './useWriterTrace'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

function Harness({ candidate }: { candidate: AgentCandidateOutput }) {
  const trace = useWriterTrace(candidate.run_id, 'succeeded', candidate)
  return <div>
    <span data-testid="state">{trace.state}</span>
    <span data-testid="mode">{trace.task?.mode ?? 'none'}</span>
    <span data-testid="activation">{trace.activation?.effective_scope ?? 'none'}</span>
    <span data-testid="materialization">{trace.materialization?.materialization_id ?? 'none'}</span>
    <span data-testid="dispatch">{trace.dispatch?.status ?? 'none'}</span>
    <span data-testid="change-set">{trace.result?.change_set_id ?? 'none'}</span>
    <span data-testid="error">{trace.error ?? 'none'}</span>
  </div>
}

describe('useWriterTrace', () => {
  afterEach(() => {
    cleanup()
    vi.unstubAllGlobals()
  })

  it('loads and binds the Writer task, candidate result, and Agent candidate', async () => {
    const trace = await writerTraceFixture('author_review_change_proposal')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse(trace.response))))

    render(<Harness candidate={trace.candidate} />)

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('ready'))
    expect(screen.getByTestId('mode')).toHaveTextContent('author_review_change_proposal')
    expect(screen.getByTestId('activation')).toHaveTextContent('project')
    expect(screen.getByTestId('materialization')).toHaveTextContent(trace.materialization.materialization_id)
    expect(screen.getByTestId('dispatch')).toHaveTextContent('committed_before_dispatch')
    expect(screen.getByTestId('change-set')).toHaveTextContent(trace.result.change_set_id!)
    expect(screen.getByTestId('error')).toHaveTextContent('none')
  })

  it('keeps a persisted Writer task visible when no result exists', async () => {
    const trace = await writerTraceFixture('repair_only')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse({
      ...trace.response,
      candidate_output: null,
      writer_candidate_result: null,
    }))))

    render(<Harness candidate={trace.candidate} />)

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('partial'))
    expect(screen.getByTestId('mode')).toHaveTextContent('repair_only')
    expect(screen.getByTestId('change-set')).toHaveTextContent('none')
  })

  it('fails closed when the candidate does not bind the persisted Writer task', async () => {
    const trace = await writerTraceFixture('author_review_change_proposal')
    const other = await writerTraceFixture('repair_only')
    vi.stubGlobal('fetch', vi.fn<typeof fetch>(() => Promise.resolve(jsonResponse(trace.response))))

    render(<Harness candidate={other.candidate} />)

    await waitFor(() => expect(screen.getByTestId('state')).toHaveTextContent('error'))
    expect(screen.getByTestId('error')).toHaveTextContent('来源链不一致')
  })
})

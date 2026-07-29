import { describe, expect, it, vi } from 'vitest'
import { agentWorkflowStateFixture } from '../test/agentWorkflowFixture'
import {
  agentCandidateOutputFixture,
  legacyAgentCandidateOutputFixture,
} from '../test/agentCandidateOutputFixture'
import { providerRecoveryFixture } from '../test/providerRecoveryFixture'
import { providerTurnReceiptListFixture } from '../test/providerTurnReceiptFixture'
import { writerTraceFixture } from '../test/writerTraceFixture'
import {
  AgentContractError,
  assertProviderTurnReceiptBindings,
  assertWriterTraceBindings,
  decideProviderRecovery,
  getAgentCandidateOutput,
  getProviderTurnReceipts,
  getProviderRecovery,
  getAgentWorkflowState,
  getWriterCandidateResult,
  getWriterTask,
  getWriterTrace,
  listAgentActivities,
  resumeAgentRun,
  type AgentActivity,
  type ProviderRecoveryResponse,
  type AgentWorkflowState,
} from './agent'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const activity: AgentActivity = {
  schema: 'zhimeng.activity-item.v1',
  activity_id: 'activity-00000005',
  session_id: 'session-one',
  run_id: 'run/a b?',
  sequence: 5,
  created_at: '2026-07-20T05:00:00Z',
  project_revision: {
    project_id: 'project-one',
    revision: 3,
    fingerprint: 'a'.repeat(64),
    observed_at: '2026-07-20T05:00:00Z',
  },
  correlation_id: 'correlation-one',
  causation_id: null,
  kind: 'tool_call',
  payload: {
    tool_call_id: 'tool-call-one',
    tool_name: 'project.read',
    arguments: { path: '05_正文内容/CH001.md' },
  },
}

const workflowIdentityCorruptions: Array<{
  label: string
  mutate: (workflow: AgentWorkflowState) => void
}> = [
  {
    label: 'response Run',
    mutate: (workflow) => { workflow.run_id = 'run-other' },
  },
  {
    label: 'Plan Run',
    mutate: (workflow) => { workflow.plan.run_id = 'run-other' },
  },
  {
    label: 'Checkpoint Plan',
    mutate: (workflow) => { workflow.checkpoints[0].workflow_plan_id = 'workflow-plan-other' },
  },
  {
    label: 'Provider boundary Run',
    mutate: (workflow) => {
      if (workflow.provider_boundary) workflow.provider_boundary.run_id = 'run-other'
    },
  },
]

interface ResumeResponseFixture {
  schema: string
  accepted: boolean
  run: { run_id: string; status: string }
  workflow_plan_id: string
  resumed_from_checkpoint_id: string | null
  resumed_from_checkpoint_hash: string | null
  next_action: string
}

describe('agent API client', () => {
  it('loads and cross-validates the immutable Writer task and candidate result', async () => {
    const trace = await writerTraceFixture('author_review_change_proposal')
    const taskFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(trace.task))
    const resultFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(trace.result))
    const candidateFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(trace.candidate))
    const traceFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(trace.response))

    const task = await getWriterTask(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher: taskFetcher },
    )
    const result = await getWriterCandidateResult(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher: resultFetcher },
    )
    const candidate = await getAgentCandidateOutput(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher: candidateFetcher },
    )
    const aggregate = await getWriterTrace(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher: traceFetcher },
    )

    expect(() => assertWriterTraceBindings(task, result, candidate)).not.toThrow()
    expect(candidate.references).toContainEqual({
      reference_kind: 'runtime_identity',
      reference_id: task.writer_task_id,
      reference_hash: task.task_hash,
    })
    expect(result.change_set_id).toMatch(/^changeset-/)
    expect(result.writes_project_truth).toBe(false)
    expect(aggregate.activation_binding?.activation_binding_id).toBe(
      trace.activation.activation_binding_id,
    )
    expect(aggregate.materialization?.materialization_id).toBe(
      trace.candidate.instruction_materialization_id,
    )
    expect(aggregate.dispatch_receipt?.dispatch_receipt_id).toBe(
      trace.candidate.instruction_dispatch_receipt_id,
    )
    expect(taskFetcher.mock.calls[0]?.[0]).toContain('/writer-task')
    expect(resultFetcher.mock.calls[0]?.[0]).toContain('/writer-candidate-result')
    expect(traceFetcher.mock.calls[0]?.[0]).toContain('/writer-trace')
  })

  it('rejects a tampered Writer task and mismatched Writer result binding', async () => {
    const trace = await writerTraceFixture('author_review_change_proposal')
    const tampered = structuredClone(trace.task)
    tampered.target_files[0]!.content_hash = '0'.repeat(64)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(tampered))

    await expect(getWriterTask(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)

    const mismatched = { ...trace.result, writer_task_hash: '0'.repeat(64) }
    expect(() => assertWriterTraceBindings(trace.task, mismatched, trace.candidate))
      .toThrow(AgentContractError)
  })

  it('loads and verifies the strict candidate output envelope for the requested Run', async () => {
    const candidate = await agentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(candidate))

    const result = await getAgentCandidateOutput(
      candidate.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result).toEqual(candidate)
    expect(result.kind).toBe('repair_plan')
    expect(result.formal_draft_allowed).toBe(false)
    expect(result.requires_author_review).toBe(true)
    expect(result.references).toHaveLength(3)
    expect(result.schema).toBe('zhimeng.agent-candidate-output.v2')
    if (result.schema !== 'zhimeng.agent-candidate-output.v2') {
      throw new Error('expected current Candidate v2')
    }
    expect(result.provider_identity_status).toBe('verified')
    expect(result.provider_turn_receipts.map((receipt) => receipt.turn_ordinal)).toEqual([1])
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/agent/runs/${candidate.run_id}/candidate-output`,
    )
  })

  it.each([
    ['Run identity', (candidate: Record<string, unknown>) => { candidate.run_id = 'run-other' }],
    ['formal draft policy', (candidate: Record<string, unknown>) => { candidate.formal_draft_allowed = true }],
    ['unknown field', (candidate: Record<string, unknown>) => { candidate.extra = 'must reject' }],
    ['content hash', (candidate: Record<string, unknown>) => { candidate.content_hash = '0'.repeat(64) }],
    ['Provider Receipt chain hash', (candidate: Record<string, unknown>) => {
      candidate.provider_turn_receipts_hash = '0'.repeat(64)
    }],
    ['Provider Receipt ordinal', (candidate: Record<string, unknown>) => {
      const receipts = candidate.provider_turn_receipts as Array<Record<string, unknown>>
      receipts[0]!.turn_ordinal = 2
    }],
    ['Provider identity status', (candidate: Record<string, unknown>) => {
      candidate.provider_identity_status = 'assumed'
    }],
  ] as const)('rejects a candidate envelope with invalid %s', async (_label, mutate) => {
    const candidate = await agentCandidateOutputFixture()
    const invalid = structuredClone(candidate) as unknown as Record<string, unknown>
    mutate(invalid)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(invalid))

    await expect(getAgentCandidateOutput(
      candidate.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it('accepts an exact historical v1 envelope without inventing Provider identity evidence', async () => {
    const candidate = await legacyAgentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(candidate))

    const result = await getAgentCandidateOutput(
      candidate.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result.schema).toBe('zhimeng.agent-candidate-output.v1')
    expect('provider_turn_receipts' in result).toBe(false)
    expect('provider_identity_status' in result).toBe(false)
  })

  it.each([
    {
      label: 'exact_match',
      options: {},
      receiptStatus: 'exact_match',
      aggregateStatus: 'verified',
    },
    {
      label: 'accepted_alias',
      options: {
        requestedModel: 'requested-model',
        reportedModel: 'gateway-model',
        acceptedReportedModels: ['gateway-model'],
      },
      receiptStatus: 'accepted_alias',
      aggregateStatus: 'verified',
    },
    {
      label: 'unreported',
      options: { reportedModel: null },
      receiptStatus: 'unreported',
      aggregateStatus: 'unreported',
    },
    {
      label: 'rejected_mismatch',
      options: { reportedModel: 'unexpected-model' },
      receiptStatus: 'rejected_mismatch',
      aggregateStatus: 'rejected_mismatch',
    },
  ] as const)('validates full Provider Receipt identity evidence for $label', async ({
    options,
    receiptStatus,
    aggregateStatus,
  }) => {
    const evidence = await providerTurnReceiptListFixture(options)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(evidence))

    const result = await getProviderTurnReceipts(
      evidence.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result.provider_turn_receipts[0]?.model_identity_status).toBe(receiptStatus)
    expect(result.provider_identity_status).toBe(aggregateStatus)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/agent/runs/${evidence.run_id}/provider-turn-receipts`,
    )
  })

  it('accepts a pending empty Receipt List but refuses to bind it to a persisted Candidate', async () => {
    const evidence = await providerTurnReceiptListFixture({ pending: true })
    const candidate = await agentCandidateOutputFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(evidence))

    const result = await getProviderTurnReceipts(
      evidence.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result.provider_identity_status).toBe('pending')
    expect(result.provider_turn_receipts).toEqual([])
    expect(() => assertProviderTurnReceiptBindings(candidate, result))
      .toThrow(AgentContractError)
  })

  it.each([
    ['cross-Run response', (response: Record<string, unknown>) => { response.run_id = 'run-other-identity' }],
    ['unknown field', (response: Record<string, unknown>) => { response.extra = 'must reject' }],
    ['chain hash', (response: Record<string, unknown>) => {
      response.provider_turn_receipts_hash = '0'.repeat(64)
    }],
    ['aggregate status', (response: Record<string, unknown>) => {
      response.provider_identity_status = 'unreported'
    }],
    ['Receipt ordinal', (response: Record<string, unknown>) => {
      const receipts = response.provider_turn_receipts as Array<Record<string, unknown>>
      receipts[0]!.turn_ordinal = 2
    }],
    ['Receipt hash', (response: Record<string, unknown>) => {
      const receipts = response.provider_turn_receipts as Array<Record<string, unknown>>
      receipts[0]!.receipt_hash = '0'.repeat(64)
    }],
    ['reported identity status', (response: Record<string, unknown>) => {
      const receipts = response.provider_turn_receipts as Array<Record<string, unknown>>
      receipts[0]!.model_identity_status = 'accepted_alias'
    }],
    ['unsorted aliases', (response: Record<string, unknown>) => {
      const receipts = response.provider_turn_receipts as Array<Record<string, unknown>>
      receipts[0]!.accepted_reported_models = ['z-model', 'a-model']
    }],
    ['invalid usage total', (response: Record<string, unknown>) => {
      const receipts = response.provider_turn_receipts as Array<Record<string, unknown>>
      const usage = receipts[0]!.usage as Record<string, unknown>
      usage.total_tokens = 1
    }],
  ] as const)('rejects full Provider Receipt evidence with invalid %s', async (_label, mutate) => {
    const evidence = structuredClone(await providerTurnReceiptListFixture()) as unknown as Record<string, unknown>
    mutate(evidence)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(evidence))

    await expect(getProviderTurnReceipts(
      'run-0123456789abcdef0123456789',
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it.each([
    { label: 'exact match', options: {} },
    {
      label: 'accepted alias',
      options: {
        requestedModel: 'requested-model',
        reportedModel: 'gateway-model',
        acceptedReportedModels: ['gateway-model'],
      },
    },
    { label: 'unreported model', options: { reportedModel: null } },
  ] as const)('cross-validates Candidate v2 against $label Receipt bindings', async ({ options }) => {
    const candidate = await agentCandidateOutputFixture(options)
    const evidence = await providerTurnReceiptListFixture(options)

    expect(() => assertProviderTurnReceiptBindings(candidate, evidence)).not.toThrow()
  })

  it('rejects cross-Run, rejected-mismatch, and legacy Candidate Receipt binding attempts', async () => {
    const candidate = await agentCandidateOutputFixture()
    const crossRun = structuredClone(await providerTurnReceiptListFixture())
    crossRun.run_id = 'run-other-identity'
    const rejected = await providerTurnReceiptListFixture({ reportedModel: 'unexpected-model' })
    const legacy = await legacyAgentCandidateOutputFixture()
    const evidence = await providerTurnReceiptListFixture()

    expect(() => assertProviderTurnReceiptBindings(candidate, crossRun)).toThrow(AgentContractError)
    expect(() => assertProviderTurnReceiptBindings(candidate, rejected)).toThrow(AgentContractError)
    expect(() => assertProviderTurnReceiptBindings(legacy, evidence)).toThrow(AgentContractError)
  })

  it('loads and verifies an immutable Provider recovery dossier', async () => {
    const recovery = await providerRecoveryFixture()
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(recovery))

    const result = await getProviderRecovery(
      recovery.dossier.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result).toEqual(recovery)
    expect(result.dossier.remote_result).toBe('unknown')
    expect(result.dossier.provider_replay_count).toBe(0)
    expect(result.dossier.instruction_dispatch_receipt_id).toMatch(
      /^creation-instruction-dispatch-/,
    )
    expect(result.dossier.instruction_dispatch_receipt_hash).toMatch(/^[0-9a-f]{64}$/)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/agent/runs/${recovery.dossier.run_id}/provider-recovery`,
    )
  })

  it('posts the exact Provider recovery CAS cursor and validates the decision receipt', async () => {
    const pending = await providerRecoveryFixture()
    const decided = await providerRecoveryFixture({ decisionAction: 'adopt_persisted_candidate' })
    const request = {
      schema: 'zhimeng.provider-recovery-decision-request.v1' as const,
      expected_dossier_revision: pending.dossier.dossier_revision,
      expected_dossier_hash: pending.dossier.dossier_hash,
      action: 'adopt_persisted_candidate' as const,
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(decided))

    const result = await decideProviderRecovery(
      pending.dossier.run_id,
      request,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result).toEqual(decided)
    expect(result.decision?.previous_dossier_hash).toBe(result.dossier.dossier_hash)
    expect(result.decision?.instruction_dispatch_receipt_id).toBe(
      result.dossier.instruction_dispatch_receipt_id,
    )
    expect(result.decision?.instruction_dispatch_receipt_hash).toBe(
      result.dossier.instruction_dispatch_receipt_hash,
    )
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      `http://127.0.0.1:8787/api/v1/agent/runs/${pending.dossier.run_id}/provider-recovery/decision`,
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual(request)
  })

  it.each([
    ['Run identity', (response: ProviderRecoveryResponse) => { response.dossier.run_id = 'run-other' }],
    ['unknown remote result', (response: ProviderRecoveryResponse) => {
      (response.dossier as unknown as Record<string, unknown>).remote_result = 'succeeded'
    }],
    ['Provider replay count', (response: ProviderRecoveryResponse) => {
      (response.dossier as unknown as Record<string, unknown>).provider_replay_count = 1
    }],
    ['Dispatch receipt identity', (response: ProviderRecoveryResponse) => {
      response.dossier.instruction_dispatch_receipt_id = 'creation-instruction-dispatch-invalid'
    }],
    ['Dispatch receipt hash', (response: ProviderRecoveryResponse) => {
      response.dossier.instruction_dispatch_receipt_hash = '0'.repeat(64)
    }],
    ['unknown field', (response: ProviderRecoveryResponse) => {
      (response.dossier as unknown as Record<string, unknown>).extra = 'must reject'
    }],
    ['Dossier hash', (response: ProviderRecoveryResponse) => { response.dossier.dossier_hash = '0'.repeat(64) }],
  ] as const)('rejects Provider recovery with invalid %s', async (_label, mutate) => {
    const recovery = structuredClone(await providerRecoveryFixture())
    mutate(recovery)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(recovery))

    await expect(getProviderRecovery(
      'run-0123456789abcdef0123456789',
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it('rejects a Provider recovery decision with a broken hash', async () => {
    const recovery = await providerRecoveryFixture({ decisionAction: 'abandon_and_recompile' })
    if (!recovery.decision) throw new Error('fixture must include a decision')
    recovery.decision.decision_hash = '0'.repeat(64)
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(recovery))

    await expect(getProviderRecovery(
      recovery.dossier.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it('rejects a valid decision receipt attached to a different immutable dossier', async () => {
    const recovery = await providerRecoveryFixture({
      decisionAction: 'adopt_persisted_candidate',
    })
    const other = await providerRecoveryFixture({
      decisionAction: 'adopt_persisted_candidate',
      instructionDispatchReceiptId: `creation-instruction-dispatch-${'8'.repeat(32)}`,
      instructionDispatchReceiptHash: '8'.repeat(64),
    })
    recovery.decision = other.decision
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(recovery))

    await expect(getProviderRecovery(
      recovery.dossier.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it('cross-binds Provider recovery to the Writer Trace instruction dispatch receipt', async () => {
    const trace = await writerTraceFixture('repair_only')
    const recovery = await providerRecoveryFixture({
      instructionDispatchReceiptId: trace.dispatch.dispatch_receipt_id,
      instructionDispatchReceiptHash: trace.dispatch.receipt_hash,
    })
    const response = { ...trace.response, provider_recovery: recovery }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response))

    const result = await getWriterTrace(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )
    expect(result.provider_recovery?.dossier.instruction_dispatch_receipt_id).toBe(
      trace.dispatch.dispatch_receipt_id,
    )

    const mismatchedRecovery = await providerRecoveryFixture()
    const mismatchedFetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      ...trace.response,
      provider_recovery: mismatchedRecovery,
    }))
    await expect(getWriterTrace(
      trace.task.run_id,
      { baseUrl: 'http://127.0.0.1:8787', fetcher: mismatchedFetcher },
    )).rejects.toBeInstanceOf(AgentContractError)
  })

  it('lists typed Run activities from the existing paged endpoint', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse({
      schema: 'zhimeng.agent-activity-list.v1',
      run_id: 'run/a b?',
      activities: [activity],
      last_sequence: 5,
    }))

    const page = await listAgentActivities(
      'run/a b?',
      4,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(page.activities).toEqual([activity])
    expect(page.last_sequence).toBe(5)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/agent/runs/run%2Fa%20b%3F/activities?after_sequence=4',
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBeUndefined()
  })

  it('loads the strict workflow state from the encoded Run endpoint', async () => {
    const workflow = agentWorkflowStateFixture({ runId: 'run/a b?', providerBoundary: true })
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(workflow))

    const result = await getAgentWorkflowState(
      'run/a b?',
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result).toEqual(workflow)
    expect(result.plan.steps).toHaveLength(3)
    expect(result.plan.steps[2]).toMatchObject({
      ordinal: 3,
      skill_id: 'novel-creation-suite.creation-handoff',
      input_builder_id: 'zhimeng.workflow-input.creation-handoff',
    })
    expect(result.checkpoints).toHaveLength(3)
    expect(result.provider_boundary).toMatchObject({
      last_step_checkpoint_id: result.checkpoints[2].checkpoint_id,
      last_step_checkpoint_hash: result.checkpoints[2].checkpoint_hash,
      status: 'dispatching',
    })
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/agent/runs/run%2Fa%20b%3F/workflow',
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBeUndefined()
  })

  it.each(workflowIdentityCorruptions)(
    'rejects a Workflow response with a mismatched $label identity',
    async ({ mutate }) => {
      const workflow = structuredClone(agentWorkflowStateFixture({
        runId: 'run/a b?',
        providerBoundary: true,
      }))
      mutate(workflow)
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(workflow))

      await expect(getAgentWorkflowState(
        'run/a b?',
        { baseUrl: 'http://127.0.0.1:8787', fetcher },
      )).rejects.toBeInstanceOf(AgentContractError)
    },
  )

  it('posts the complete workflow CAS cursor when resuming a Run', async () => {
    const workflow = agentWorkflowStateFixture({ runId: 'run/a b?' })
    const request = {
      schema: 'zhimeng.agent-run-resume-request.v1' as const,
      expected_workflow_plan_id: workflow.plan.workflow_plan_id,
      expected_plan_hash: workflow.plan.plan_hash,
      expected_checkpoint_id: workflow.last_checkpoint_id,
      expected_checkpoint_hash: workflow.last_checkpoint_hash,
    }
    const response = {
      schema: 'zhimeng.agent-run-resume.v1',
      accepted: true,
      run: { run_id: 'run/a b?', status: 'running' },
      workflow_plan_id: workflow.plan.workflow_plan_id,
      resumed_from_checkpoint_id: workflow.last_checkpoint_id,
      resumed_from_checkpoint_hash: workflow.last_checkpoint_hash,
      next_action: 'workflow_step',
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response))

    const result = await resumeAgentRun(
      'run/a b?',
      request,
      { baseUrl: 'http://127.0.0.1:8787', fetcher },
    )

    expect(result).toEqual(response)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/agent/runs/run%2Fa%20b%3F/resume',
    )
    expect(fetcher.mock.calls[0]?.[1]?.method).toBe('POST')
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual(request)
  })

  it.each([
    ['Run', (response: ResumeResponseFixture) => { response.run.run_id = 'run-other' }],
    ['Plan', (response: ResumeResponseFixture) => { response.workflow_plan_id = 'workflow-plan-other' }],
    ['Checkpoint', (response: ResumeResponseFixture) => { response.resumed_from_checkpoint_hash = '0'.repeat(64) }],
  ] as const)(
    'rejects a Resume response that does not echo the submitted %s identity',
    async (_label, mutate) => {
      const workflow = agentWorkflowStateFixture({ runId: 'run/a b?' })
      const request = {
        schema: 'zhimeng.agent-run-resume-request.v1' as const,
        expected_workflow_plan_id: workflow.plan.workflow_plan_id,
        expected_plan_hash: workflow.plan.plan_hash,
        expected_checkpoint_id: workflow.last_checkpoint_id,
        expected_checkpoint_hash: workflow.last_checkpoint_hash,
      }
      const response: ResumeResponseFixture = {
        schema: 'zhimeng.agent-run-resume.v1',
        accepted: true,
        run: { run_id: 'run/a b?', status: 'running' },
        workflow_plan_id: workflow.plan.workflow_plan_id,
        resumed_from_checkpoint_id: workflow.last_checkpoint_id,
        resumed_from_checkpoint_hash: workflow.last_checkpoint_hash,
        next_action: 'workflow_step',
      }
      mutate(response)
      const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(response))

      await expect(resumeAgentRun(
        'run/a b?',
        request,
        { baseUrl: 'http://127.0.0.1:8787', fetcher },
      )).rejects.toBeInstanceOf(AgentContractError)
    },
  )
})

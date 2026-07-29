import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { FourSkillsInstructionChainPanel } from './FourSkillsInstructionChainPanel'

const batchId = 'four-skills-raw-adaptation-' + 'a'.repeat(32)
const bindingId = 'four-skills-workflow-binding-' + 'b'.repeat(32)
const orchestrationId = 'four-skills-workflow-orchestration-' + 'c'.repeat(32)
const handoffId = 'four-skills-instruction-chain-handoff-' + 'd'.repeat(32)
const probeId = 'four-skills-generic-execution-probe-' + 'f'.repeat(32)
const hash = 'e'.repeat(64)

function member(family: string, ordinal: number) {
  return {
    family,
    ordinal,
    source_path: `C:/skills/${family}`,
    plan_id: `plan-${ordinal}`,
    plan_hash: hash,
    materialization_id: `mat-${ordinal}`,
    skill_id: `adapted.${family}`,
    skill_version_id: `skill-version-${ordinal}${'0'.repeat(24)}`.slice(0, 45),
    package_fingerprint: hash,
    present: true,
    linked: true,
    executable: false,
    formal_draft_allowed: false,
    detail: 'ok',
  }
}

const planned = {
  schema: 'zhimeng.four-skills-raw-adaptation-response.v1',
  receipt: {
    schema: 'zhimeng.four-skills-raw-adaptation-receipt.v1',
    batch_id: batchId,
    status: 'planned',
    members: [
      member('source_ingest', 1),
      member('distillation', 2),
      member('knowledge_context', 3),
      member('creation_suite', 4),
    ],
    member_count: 4,
    all_present: true,
    all_linked: true,
    authorization_input_ready: false,
    authorization_unlock: false,
    formal_draft_allowed: false,
    writes_project_truth: false,
    four_skills_generic_exactly_once: false,
    raw_skill_md_execution: false,
    policy_version: 'local-v1-four-skills-raw-adaptation-1',
    detail: 'planned',
    updated_at: '2026-07-26T00:00:00+00:00',
    batch_hash: hash,
  },
  not_claiming: ['four_skills_generic_exactly_once', 'raw_skill_md_execution', 'managed_skill_execution', 'local_v1_exe'],
}

const materialized = {
  ...planned,
  receipt: {
    ...planned.receipt,
    status: 'materialized',
    authorization_input_ready: true,
    detail: 'materialized',
  },
}

const bound = {
  schema: 'zhimeng.four-skills-workflow-binding-response.v1',
  receipt: {
    schema: 'zhimeng.four-skills-workflow-binding-receipt.v1',
    binding_id: bindingId,
    batch_id: batchId,
    batch_hash: hash,
    status: 'bound',
    workflow_surface: 'generic_instruction_workflow',
    steps: planned.receipt.members.map((item) => ({
      family: item.family,
      ordinal: item.ordinal,
      skill_id: item.skill_id,
      skill_version_id: item.skill_version_id,
      package_fingerprint: hash,
      runner: 'instruction_source',
      executable: false,
      bound: true,
      detail: 'bound',
    })),
    step_count: 4,
    all_bound: true,
    workflow_ready: true,
    executable: false,
    raw_skill_md_execution: false,
    four_skills_generic_exactly_once: false,
    formal_draft_allowed: false,
    writes_project_truth: false,
    policy_version: 'local-v1-four-skills-workflow-binding-1',
    detail: 'bound',
    bound_at: '2026-07-26T00:01:00+00:00',
    binding_hash: hash,
  },
  not_claiming: planned.not_claiming,
}

const orchestrated = {
  schema: 'zhimeng.four-skills-workflow-orchestration-response.v1',
  receipt: {
    schema: 'zhimeng.four-skills-workflow-orchestration-receipt.v1',
    orchestration_id: orchestrationId,
    batch_id: batchId,
    batch_hash: hash,
    binding_id: bindingId,
    binding_hash: hash,
    status: 'completed',
    workflow_surface: 'generic_instruction_workflow',
    steps: planned.receipt.members.map((item) => ({
      family: item.family,
      ordinal: item.ordinal,
      skill_id: item.skill_id,
      skill_version_id: item.skill_version_id,
      package_fingerprint: hash,
      runner: 'instruction_orchestrator',
      status: 'completed',
      managed_skill_execution: false,
      raw_skill_md_execution: false,
      detail: 'completed',
    })),
    step_count: 4,
    completed_step_count: 4,
    orchestration_ready: true,
    runnable: true,
    managed_skill_execution: false,
    raw_skill_md_execution: false,
    four_skills_generic_exactly_once: false,
    formal_draft_allowed: false,
    writes_project_truth: false,
    policy_version: 'local-v1-four-skills-workflow-orchestration-1',
    detail: 'completed',
    orchestrated_at: '2026-07-26T00:02:00+00:00',
    orchestration_hash: hash,
  },
  not_claiming: planned.not_claiming,
}

const handed = {
  schema: 'zhimeng.four-skills-instruction-chain-handoff-response.v1',
  receipt: {
    schema: 'zhimeng.four-skills-instruction-chain-handoff-receipt.v1',
    handoff_id: handoffId,
    batch_id: batchId,
    batch_hash: hash,
    binding_id: bindingId,
    binding_hash: hash,
    orchestration_id: orchestrationId,
    orchestration_hash: hash,
    status: 'ready',
    workflow_surface: 'generic_instruction_workflow',
    steps: planned.receipt.members.map((item) => ({
      family: item.family,
      ordinal: item.ordinal,
      skill_id: item.skill_id,
      skill_version_id: item.skill_version_id,
      package_fingerprint: hash,
      runner: 'instruction_chain_handoff',
      status: 'prepared',
      managed_skill_execution: false,
      raw_skill_md_execution: false,
      detail: 'prepared',
    })),
    step_count: 4,
    prepared_step_count: 4,
    instruction_chain_ready: true,
    managed_skill_execution: false,
    raw_skill_md_execution: false,
    four_skills_generic_exactly_once: false,
    formal_draft_allowed: false,
    writes_project_truth: false,
    policy_version: 'local-v1-four-skills-instruction-chain-handoff-1',
    detail: 'ready',
    handed_off_at: '2026-07-26T00:03:00+00:00',
    handoff_hash: hash,
  },
  not_claiming: planned.not_claiming,
}

const probed = {
  schema: 'zhimeng.four-skills-generic-execution-probe-response.v1',
  receipt: {
    schema: 'zhimeng.four-skills-generic-execution-probe-receipt.v1',
    probe_id: probeId,
    batch_id: batchId,
    batch_hash: hash,
    handoff_id: handoffId,
    handoff_hash: hash,
    status: 'complete',
    steps: planned.receipt.members.map((item) => ({
      family: item.family,
      ordinal: item.ordinal,
      skill_id: item.skill_id,
      skill_version_id: item.skill_version_id,
      package_fingerprint: hash,
      package_kind: 'instruction_source',
      catalog_present: true,
      fingerprint_matched: true,
      executor_would_accept: false,
      denial_code: 'skill_not_executable',
      status: 'denied',
      managed_skill_execution: false,
      raw_skill_md_execution: false,
      detail: 'denied',
    })),
    step_count: 4,
    assessed_step_count: 4,
    denied_step_count: 4,
    would_accept_step_count: 0,
    probe_complete: true,
    generic_execution_ready: false,
    managed_skill_execution: false,
    raw_skill_md_execution: false,
    four_skills_generic_exactly_once: false,
    formal_draft_allowed: false,
    writes_project_truth: false,
    policy_version: 'local-v1-four-skills-generic-execution-probe-1',
    detail: 'probe complete',
    probed_at: '2026-07-26T00:04:00+00:00',
    probe_hash: hash,
  },
  not_claiming: [...planned.not_claiming, 'generic_execution_ready'],
}

describe('FourSkillsInstructionChainPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('runs plan/materialize/bind/orchestrate/handoff/probe without project managed execution', async () => {
    const user = userEvent.setup()
    const onImported = vi.fn()
    vi.mocked(globalThis.fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify(planned), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(materialized), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(bound), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(orchestrated), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(handed), { status: 201, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(probed), { status: 201, headers: { 'Content-Type': 'application/json' } }))

    render(<FourSkillsInstructionChainPanel enabled onImported={onImported} />)

    await user.type(screen.getByLabelText('源导入'), 'C:\\skills\\source')
    await user.type(screen.getByLabelText('蒸馏'), 'C:\\skills\\distill')
    await user.type(screen.getByLabelText('知识上下文'), 'C:\\skills\\kb')
    await user.type(screen.getByLabelText('Creation Suite'), 'C:\\skills\\creation')
    await user.click(screen.getByRole('button', { name: '跑完整 Instruction 链' }))

    await waitFor(() => {
      expect(screen.getByText(/Instruction 链已交接/)).toBeInTheDocument()
    })
    expect(screen.getByText(/instruction_chain_ready/)).toBeInTheDocument()
    expect(screen.getAllByText('false').length).toBeGreaterThan(0)
    expect(onImported).toHaveBeenCalled()
    const urls = vi.mocked(globalThis.fetch).mock.calls.map((call) => String(call[0]))
    expect(urls.some((url) => url.includes('/four-skills-raw-adaptations/plan'))).toBe(true)
    expect(urls.some((url) => url.includes('/instruction-chain-handoffs'))).toBe(true)
    expect(urls.some((url) => url.includes('/generic-execution-probes'))).toBe(true)
    expect(screen.getAllByText(/generic_execution_ready/).length).toBeGreaterThan(0)
    expect(screen.getByText('执行探测')).toBeInTheDocument()
  })
})

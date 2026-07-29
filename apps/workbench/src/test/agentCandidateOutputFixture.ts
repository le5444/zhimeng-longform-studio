import type {
  AgentCandidateOutput,
  AgentCandidateOutputV1,
} from '../api/agent'
import { canonicalJsonHash } from '../lib/canonicalJson'
import {
  providerTurnReceiptFixture,
  type ProviderTurnReceiptFixtureOptions,
} from './providerTurnReceiptFixture'

const runId = 'run-0123456789abcdef0123456789'
const workflowPlanId = `workflow-plan-${'a'.repeat(32)}`
const finalCheckpointId = `workflow-checkpoint-${'b'.repeat(32)}`
const providerBoundaryId = `provider-io-${'c'.repeat(32)}`
const operationPackageId = `prewrite-package-${'d'.repeat(32)}`

export async function agentCandidateOutputFixture(
  providerOptions: ProviderTurnReceiptFixtureOptions = {},
): Promise<AgentCandidateOutput> {
  const content = {
    title: '写前修复方案',
    summary: '当前门禁阻止正式正文，先补齐场景压力与线索绑定。',
    body: '## 修复建议\n\n- 将周鹤亭的压力放入开场场景。\n- 保留黄铜钥匙与葬礼女人的线索。',
    format: 'markdown' as const,
  }
  const references = [
    {
      reference_kind: 'workflow_checkpoint' as const,
      reference_id: finalCheckpointId,
      reference_hash: 'b'.repeat(64),
    },
    {
      reference_kind: 'runtime_identity' as const,
      reference_id: 'context-pack-candidate-fixture',
      reference_hash: 'e'.repeat(64),
    },
    {
      reference_kind: 'runtime_identity' as const,
      reference_id: operationPackageId,
      reference_hash: 'f'.repeat(64),
    },
  ]
  const providerTurnReceipt = await providerTurnReceiptFixture(providerOptions)
  if (providerTurnReceipt.model_identity_status === 'rejected_mismatch') {
    throw new Error('Runtime must reject a Provider model mismatch before Candidate creation')
  }
  const providerTurnReceipts = [{
    turn_ordinal: providerTurnReceipt.turn_ordinal,
    receipt_id: providerTurnReceipt.receipt_id,
    receipt_hash: providerTurnReceipt.receipt_hash,
  }]
  const candidateOutputId = `agent-candidate-${(await canonicalJsonHash({
    schema: 'zhimeng.agent-candidate-output.v2',
    run_id: runId,
    provider_boundary_id: providerBoundaryId,
  })).slice(0, 32)}`
  const material = {
    schema: 'zhimeng.agent-candidate-output.v2' as const,
    candidate_output_id: candidateOutputId,
    run_id: runId,
    workflow_plan_id: workflowPlanId,
    workflow_plan_hash: 'a'.repeat(64),
    final_checkpoint_id: finalCheckpointId,
    final_checkpoint_hash: 'b'.repeat(64),
    provider_boundary_id: providerBoundaryId,
    provider_boundary_hash: 'c'.repeat(64),
    provider_request_hash: 'd'.repeat(64),
    context_pack_id: 'context-pack-candidate-fixture',
    context_pack_hash: 'e'.repeat(64),
    operation_package_id: operationPackageId,
    operation_package_hash: 'f'.repeat(64),
    project_id: 'project-0123456789abcdef01234567',
    project_revision: 3,
    project_fingerprint: '0'.repeat(64),
    handoff_status: 'risk_draft_only' as const,
    allowed_output: 'repair_plan_or_risk_draft_only' as const,
    failure_signatures: ['prewrite_gate_pressure_missing'],
    kind: 'repair_plan' as const,
    content,
    content_hash: await canonicalJsonHash(content),
    references,
    references_hash: await canonicalJsonHash(references),
    provider_turn_receipts: providerTurnReceipts,
    provider_turn_receipts_hash: await canonicalJsonHash(providerTurnReceipts),
    provider_identity_status: providerTurnReceipt.model_identity_status === 'unreported'
      ? 'unreported' as const
      : 'verified' as const,
    formal_draft_allowed: false as const,
    requires_author_review: true as const,
    created_at: '2026-07-22T01:00:00Z',
  }
  return {
    ...material,
    envelope_hash: await canonicalJsonHash(material),
  }
}

export async function legacyAgentCandidateOutputFixture(): Promise<AgentCandidateOutputV1> {
  const current = await agentCandidateOutputFixture()
  if (current.schema !== 'zhimeng.agent-candidate-output.v2') {
    throw new Error('current Candidate fixture must use the v2 contract')
  }
  const {
    provider_turn_receipts: _providerTurnReceipts,
    provider_turn_receipts_hash: _providerTurnReceiptsHash,
    provider_identity_status: _providerIdentityStatus,
    envelope_hash: _currentEnvelopeHash,
    candidate_output_id: _currentCandidateOutputId,
    schema: _currentSchema,
    ...shared
  } = current
  const material = {
    ...shared,
    schema: 'zhimeng.agent-candidate-output.v1' as const,
    candidate_output_id: `agent-candidate-${(await canonicalJsonHash({
      schema: 'zhimeng.agent-candidate-output.v1',
      run_id: current.run_id,
      provider_boundary_id: current.provider_boundary_id,
    })).slice(0, 32)}`,
  }
  return {
    ...material,
    envelope_hash: await canonicalJsonHash(material),
  }
}

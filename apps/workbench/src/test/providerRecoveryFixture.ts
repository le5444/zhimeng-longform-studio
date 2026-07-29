import type {
  ProviderRecoveryAction,
  ProviderRecoveryDecision,
  ProviderRecoveryDossier,
  ProviderRecoveryResponse,
} from '../api/agent'
import { canonicalJsonHash } from '../lib/canonicalJson'
import { agentCandidateOutputFixture } from './agentCandidateOutputFixture'

const runId = 'run-0123456789abcdef0123456789'
const providerBoundaryId = `provider-io-${'c'.repeat(32)}`

export async function providerRecoveryDecisionFixture(
  dossier: ProviderRecoveryDossier,
  action: ProviderRecoveryAction,
): Promise<ProviderRecoveryDecision> {
  const decisionMaterial = {
    schema: 'zhimeng.provider-recovery-decision.v1' as const,
    decision_id: `provider-recovery-decision-${(await canonicalJsonHash({
      schema: 'zhimeng.provider-recovery-decision.v1',
      dossier_id: dossier.dossier_id,
      previous_dossier_hash: dossier.dossier_hash,
      instruction_dispatch_receipt_id: dossier.instruction_dispatch_receipt_id,
      instruction_dispatch_receipt_hash: dossier.instruction_dispatch_receipt_hash,
      action,
      previous_dossier_revision: dossier.dossier_revision,
    })).slice(0, 32)}`,
    dossier_id: dossier.dossier_id,
    run_id: dossier.run_id,
    previous_dossier_hash: dossier.dossier_hash,
    instruction_dispatch_receipt_id: dossier.instruction_dispatch_receipt_id,
    instruction_dispatch_receipt_hash: dossier.instruction_dispatch_receipt_hash,
    action,
    previous_status: 'pending' as const,
    resulting_run_status: action === 'adopt_persisted_candidate' ? 'succeeded' as const : 'failed' as const,
    previous_dossier_revision: dossier.dossier_revision,
    new_dossier_revision: dossier.dossier_revision + 1,
    created_at: new Date(Date.parse(dossier.created_at) + 60_000).toISOString(),
  }
  return {
    ...decisionMaterial,
    decision_hash: await canonicalJsonHash(decisionMaterial),
  }
}

export async function providerRecoveryFixture({
  candidateState = 'persisted',
  decisionAction = null,
  instructionDispatchReceiptId = `creation-instruction-dispatch-${'9'.repeat(32)}`,
  instructionDispatchReceiptHash = '9'.repeat(64),
}: {
  candidateState?: 'persisted' | 'absent' | 'invalid'
  decisionAction?: ProviderRecoveryAction | null
  instructionDispatchReceiptId?: string
  instructionDispatchReceiptHash?: string
} = {}): Promise<ProviderRecoveryResponse> {
  const candidate = candidateState === 'persisted'
    ? await agentCandidateOutputFixture()
    : null
  const dossierId = `provider-recovery-${(await canonicalJsonHash({
    schema: 'zhimeng.provider-recovery-dossier.v1',
    run_id: runId,
    provider_boundary_id: providerBoundaryId,
  })).slice(0, 32)}`
  const dossierMaterial = {
    schema: 'zhimeng.provider-recovery-dossier.v1' as const,
    dossier_id: dossierId,
    run_id: runId,
    workflow_plan_id: `workflow-plan-${'a'.repeat(32)}`,
    workflow_plan_hash: 'a'.repeat(64),
    final_checkpoint_id: `workflow-checkpoint-${'b'.repeat(32)}`,
    final_checkpoint_hash: 'b'.repeat(64),
    provider_boundary_id: providerBoundaryId,
    provider_boundary_hash: 'c'.repeat(64),
    provider_request_hash: 'd'.repeat(64),
    instruction_dispatch_receipt_id: instructionDispatchReceiptId,
    instruction_dispatch_receipt_hash: instructionDispatchReceiptHash,
    context_pack_id: 'context-pack-candidate-fixture',
    context_pack_hash: 'e'.repeat(64),
    operation_package_id: `prewrite-package-${'d'.repeat(32)}`,
    operation_package_hash: 'f'.repeat(64),
    project_id: 'project-0123456789abcdef01234567',
    project_revision: 3,
    project_fingerprint: '0'.repeat(64),
    run_status_at_capture: 'interrupted' as const,
    candidate_state: candidateState,
    candidate_output_id: candidate?.candidate_output_id ?? null,
    candidate_output_hash: candidate?.envelope_hash ?? null,
    remote_result: 'unknown' as const,
    provider_replay_count: 0 as const,
    dossier_revision: 1,
    created_at: '2026-07-22T02:30:00Z',
  }
  const dossier = {
    ...dossierMaterial,
    dossier_hash: await canonicalJsonHash(dossierMaterial),
  }
  if (!decisionAction) return {
    schema: 'zhimeng.provider-recovery.v1',
    dossier,
    decision: null,
  }
  return {
    schema: 'zhimeng.provider-recovery.v1',
    dossier,
    decision: await providerRecoveryDecisionFixture(dossier, decisionAction),
  }
}

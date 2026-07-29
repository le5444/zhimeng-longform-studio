import type {
  DistillationInvocationReceipt,
  DistillationInvocationSnapshot,
  DistillationInvocationStatus,
} from '../api/distillation'

export const runtimeDistillationInvocationId = `distillation-invocation-${'1'.repeat(32)}`

const baseReceiptHash = 'a'.repeat(64)

function receipt(
  ordinal: number,
  kind: DistillationInvocationReceipt['kind'],
  status: DistillationInvocationStatus,
  payloadSchema: string,
): DistillationInvocationReceipt {
  return {
    schema: 'zhimeng.distillation-invocation-receipt.v1',
    receipt_id: `distillation-receipt-${String(ordinal).repeat(32).slice(0, 32)}`,
    invocation_id: runtimeDistillationInvocationId,
    ordinal,
    kind,
    turn_ordinal: kind === 'provider_dispatch' || kind === 'provider_turn_result' ? Math.ceil((ordinal - 1) / 2) : null,
    parent_receipt_id: ordinal === 1
      ? null
      : `distillation-receipt-${String(ordinal - 1).repeat(32).slice(0, 32)}`,
    expected_invocation_revision: ordinal - 1,
    resulting_invocation_revision: ordinal,
    resulting_status: status,
    idempotency_key: `distillation-receipt-idempotency-${String(ordinal).repeat(32).slice(0, 32)}`,
    payload: { schema: payloadSchema },
    payload_hash: String(ordinal).repeat(64).slice(0, 64),
    created_at: `2026-07-23T00:0${ordinal}:00Z`,
    receipt_hash: String.fromCharCode(96 + ordinal).repeat(64),
  }
}

export function distillationInvocationSnapshotFixture(
  options: {
    invocationId?: string
    status?: DistillationInvocationStatus
    withRecovery?: boolean
  } = {},
): DistillationInvocationSnapshot {
  const invocationId = options.invocationId ?? runtimeDistillationInvocationId
  const status = options.status ?? 'output_persisted'
  const receipts = [
    receipt(1, 'prepared', 'prepared', 'zhimeng.distillation-prepared.v1'),
    receipt(2, 'provider_dispatch', 'running', 'zhimeng.distillation-provider-dispatch.v1'),
    receipt(3, 'provider_turn_result', 'running', 'zhimeng.distillation-provider-turn-result.v1'),
    receipt(4, 'candidate_output', 'output_persisted', 'zhimeng.distillation-candidate-output.v1'),
  ].map((item) => ({ ...item, invocation_id: invocationId }))
  const lastReceipt = receipts[receipts.length - 1]

  return {
    schema: 'zhimeng.distillation-invocation-snapshot.v1',
    last_receipt_id: lastReceipt.receipt_id,
    receipts,
    invocation: {
      schema: 'zhimeng.distillation-invocation.v1',
      invocation_id: invocationId,
      source: {
        source_import_id: `source-import-${'9'.repeat(32)}`,
        source_receipt_id: `source-import-receipt-${'9'.repeat(32)}`,
      },
      project: {
        project_id: `project-${'8'.repeat(24)}`,
        project_revision: 7,
      },
      knowledge: {
        scope_id: 'project:sample',
        scope_state_revision: 3,
      },
      instruction: {
        skill_id: 'novel-distillation.deep-instruction',
        skill_version: '1.0.0',
      },
      provider: {
        requested_model: 'fixture-model',
        tool_definitions_hash: '2'.repeat(64),
      },
      anchor_catalog_id: `distillation-anchor-catalog-${'7'.repeat(32)}`,
      anchor_catalog_hash: '7'.repeat(64),
      retry_of: null,
      request_hash: '8'.repeat(64),
      idempotency_key: `distillation-idempotency-${'1'.repeat(32)}`,
      status,
      state_revision: lastReceipt.ordinal,
      provider_replay_count: 0,
      last_receipt_ordinal: lastReceipt.ordinal,
      last_receipt_hash: lastReceipt.receipt_hash,
      last_dispatch_receipt_id: receipts[1].receipt_id,
      last_dispatch_receipt_hash: receipts[1].receipt_hash,
      last_dispatch_turn_ordinal: 1,
      last_turn_result_receipt_id: receipts[2].receipt_id,
      last_turn_result_receipt_hash: receipts[2].receipt_hash,
      provider_turn_count: 1,
      provider_turn_chain_hash: '3'.repeat(64),
      candidate_output_receipt_id: receipts[3].receipt_id,
      candidate_output_receipt_hash: receipts[3].receipt_hash,
      recovery_dossier_receipt_id: options.withRecovery
        ? `distillation-receipt-${'5'.repeat(32)}`
        : null,
      recovery_dossier_revision: options.withRecovery ? 1 : null,
      recovery_dossier_receipt_hash: options.withRecovery ? '5'.repeat(64) : null,
      recovery_decision_receipt_id: null,
      recovery_decision_receipt_hash: null,
      final_result_receipt_id: null,
      final_result_receipt_hash: null,
      candidate_set_hash: '4'.repeat(64),
      work_count: 1,
      voice_count: 1,
      validator_count: 1,
      created_at: '2026-07-23T00:00:00Z',
      updated_at: '2026-07-23T00:04:00Z',
      head_hash: baseReceiptHash,
    },
  }
}

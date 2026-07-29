import type {
  ProviderIdentityAggregateStatus,
  ProviderModelIdentityStatus,
  ProviderTurnReceipt,
  ProviderTurnReceiptListResponse,
} from '../api/agent'
import { canonicalJsonHash } from '../lib/canonicalJson'

export const providerReceiptRunId = 'run-0123456789abcdef0123456789'
export const providerReceiptBoundaryId = `provider-io-${'c'.repeat(32)}`
export const providerReceiptBoundaryHash = 'c'.repeat(64)
export const providerReceiptRequestHash = 'd'.repeat(64)

export interface ProviderTurnReceiptFixtureOptions {
  requestedModel?: string | null
  reportedModel?: string | null
  acceptedReportedModels?: readonly string[]
  pending?: boolean
}

function identityStatus(
  requestedModel: string | null,
  reportedModel: string | null,
  acceptedReportedModels: string[],
): ProviderModelIdentityStatus {
  if (reportedModel === null) return 'unreported'
  if (requestedModel !== null && reportedModel === requestedModel) return 'exact_match'
  if (acceptedReportedModels.includes(reportedModel)) return 'accepted_alias'
  return 'rejected_mismatch'
}

export async function providerTurnReceiptFixture(
  options: ProviderTurnReceiptFixtureOptions = {},
): Promise<ProviderTurnReceipt> {
  const requestedModel = options.requestedModel === undefined
    ? 'playwright-scripted-model'
    : options.requestedModel
  const reportedModel = options.reportedModel === undefined
    ? requestedModel
    : options.reportedModel
  const acceptedReportedModels = [...(options.acceptedReportedModels ?? [])].sort()
  const turnOrdinal = 1
  const receiptId = `provider-turn-receipt-${(await canonicalJsonHash({
    schema: 'zhimeng.provider-turn-receipt.v1',
    run_id: providerReceiptRunId,
    provider_boundary_id: providerReceiptBoundaryId,
    turn_ordinal: turnOrdinal,
    request_hash: providerReceiptRequestHash,
  })).slice(0, 32)}`
  const material = {
    schema: 'zhimeng.provider-turn-receipt.v1' as const,
    receipt_id: receiptId,
    run_id: providerReceiptRunId,
    provider_boundary_id: providerReceiptBoundaryId,
    provider_boundary_hash: providerReceiptBoundaryHash,
    turn_ordinal: turnOrdinal,
    request_hash: providerReceiptRequestHash,
    requested_model: requestedModel,
    reported_model: reportedModel,
    accepted_reported_models: acceptedReportedModels,
    model_identity_status: identityStatus(
      requestedModel,
      reportedModel,
      acceptedReportedModels,
    ),
    finish_reason: 'tool_calls',
    usage: { input_tokens: 120, output_tokens: 80, total_tokens: 200 },
    created_at: '2026-07-22T00:59:59Z',
  }
  return {
    ...material,
    receipt_hash: await canonicalJsonHash(material),
  }
}

export async function providerTurnReceiptListFixture(
  options: ProviderTurnReceiptFixtureOptions = {},
): Promise<ProviderTurnReceiptListResponse> {
  if (options.pending) {
    const requestedModel = options.requestedModel === undefined
      ? 'playwright-scripted-model'
      : options.requestedModel
    return {
      schema: 'zhimeng.provider-turn-receipt-list.v1',
      run_id: providerReceiptRunId,
      provider_boundary_id: providerReceiptBoundaryId,
      provider_boundary_hash: providerReceiptBoundaryHash,
      requested_model: requestedModel,
      provider_turn_receipts: [],
      provider_turn_receipts_hash: await canonicalJsonHash([]),
      provider_identity_status: 'pending',
    }
  }
  const receipt = await providerTurnReceiptFixture(options)
  const bindings = [{
    turn_ordinal: receipt.turn_ordinal,
    receipt_id: receipt.receipt_id,
    receipt_hash: receipt.receipt_hash,
  }]
  const aggregateStatus: ProviderIdentityAggregateStatus = receipt.model_identity_status === 'rejected_mismatch'
    ? 'rejected_mismatch'
    : receipt.model_identity_status === 'unreported'
      ? 'unreported'
      : 'verified'
  return {
    schema: 'zhimeng.provider-turn-receipt-list.v1',
    run_id: receipt.run_id,
    provider_boundary_id: receipt.provider_boundary_id,
    provider_boundary_hash: receipt.provider_boundary_hash,
    requested_model: receipt.requested_model,
    provider_turn_receipts: [receipt],
    provider_turn_receipts_hash: await canonicalJsonHash(bindings),
    provider_identity_status: aggregateStatus,
  }
}

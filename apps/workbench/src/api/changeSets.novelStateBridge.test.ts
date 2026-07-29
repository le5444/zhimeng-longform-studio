import { describe, expect, it, vi } from 'vitest'
import {
  exportOriginalNlclNovelStateBridge,
  exportOriginalNlclNovelStateSync,
  getOriginalNlclNovelStateBridge,
  getOriginalNlclNovelStateSync,
  getOriginalNlclPrewriteContextReport,
  exportOriginalNlclPrewriteContextReport,
  getOriginalNlclTruthContentExtract,
  exportOriginalNlclTruthContentExtract,
  getOriginalNlclMandatoryUsagePlan,
  exportOriginalNlclMandatoryUsagePlan,
  getOriginalNlclMandatoryUsageClosure,
  exportOriginalNlclMandatoryUsageClosure,
  getOriginalNlclOperationPackageLike,
  exportOriginalNlclOperationPackageLike,
  getOriginalNlclLibraryLayoutInventory,
  exportOriginalNlclLibraryLayoutInventory,
  getOriginalNlclLibraryLayoutCompleteness,
  exportOriginalNlclLibraryLayoutCompleteness,
  getOriginalNlclBoundedLibraryActivation,
  exportOriginalNlclBoundedLibraryActivation,
} from './changeSets'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const receipt = {
  schema: 'zhimeng.nlcl-original-novel-state-bridge-receipt.v1' as const,
  receipt_id: 'nlcl-original-novel-state-bridge-' + 'a'.repeat(32),
  change_set_id: 'changeset-' + 'b'.repeat(32),
  project_id: 'project-' + 'c'.repeat(24),
  nlcl_original_chapter_ops_usage_export_receipt_id:
    'nlcl-original-chapter-ops-usage-' + 'd'.repeat(32),
  nlcl_original_chapter_ops_usage_export_receipt_hash: 'e'.repeat(64),
  nlcl_original_call_usage_export_receipt_id:
    'nlcl-original-call-usage-' + 'f'.repeat(32),
  nlcl_original_call_usage_export_receipt_hash: '1'.repeat(64),
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  chapter_relative_paths: ['05_正文内容/第一卷/CH001_正文.md'],
  character_state_refs: [
    {
      relative_path: '03_人物设定/人物.json',
      source_hash: '2'.repeat(64),
      domain: 'character' as const,
    },
  ],
  plot_state_refs: [],
  foreshadowing_state_refs: [],
  project_status_refs: [],
  source_run_id: null,
  export_relative_path: '07_运营数据/nlcl_feedback/novel_state_bridge.jsonl',
  export_absolute_path: 'C:/tmp/novel_state_bridge.jsonl',
  export_file_sha256: '3'.repeat(64),
  export_byte_offset: 0,
  export_row_sha256: '4'.repeat(64),
  record_mode: 'product_bridge_novel_state_inventory_from_chapter_ops' as const,
  original_nlcl_call_usage_recorded: true as const,
  original_nlcl_chapter_ops_usage_synced: true as const,
  original_nlcl_novel_state_bridge_recorded: true as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: false as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: '5'.repeat(64),
}

describe('NLCL novel-state bridge API client', () => {
  it('gets novel-state bridge response', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        schema: 'zhimeng.nlcl-original-novel-state-bridge-response.v1',
        change_set_id: receipt.change_set_id,
        receipt,
        history: [receipt],
      }),
    )
    const result = await getOriginalNlclNovelStateBridge(receipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(result.receipt?.original_nlcl_novel_state_bridge_recorded).toBe(true)
    expect(result.receipt?.novel_state_synced).toBe(false)
    expect(fetcher).toHaveBeenCalledWith(
      `http://127.0.0.1:8787/api/v1/change-sets/${receipt.change_set_id}/nlcl-original-novel-state-bridge`,
      expect.anything(),
    )
  })

  it('posts export with expected chapter/ops hash', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(
      jsonResponse({
        schema: 'zhimeng.nlcl-original-novel-state-bridge-response.v1',
        change_set_id: receipt.change_set_id,
        receipt,
        history: [receipt],
      }),
    )
    await exportOriginalNlclNovelStateBridge(
      receipt.change_set_id,
      receipt.nlcl_original_chapter_ops_usage_export_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      expected_nlcl_original_chapter_ops_usage_export_receipt_hash:
        receipt.nlcl_original_chapter_ops_usage_export_receipt_hash,
    })
  })
})

const syncReceipt = {
  schema: 'zhimeng.nlcl-original-novel-state-sync-receipt.v1' as const,
  receipt_id: 'nlcl-original-novel-state-sync-' + 'a'.repeat(32),
  change_set_id: receipt.change_set_id,
  project_id: receipt.project_id,
  nlcl_original_novel_state_bridge_receipt_id: receipt.receipt_id,
  nlcl_original_novel_state_bridge_receipt_hash: receipt.receipt_hash,
  nlcl_original_chapter_ops_usage_export_receipt_id:
    receipt.nlcl_original_chapter_ops_usage_export_receipt_id,
  nlcl_original_chapter_ops_usage_export_receipt_hash:
    receipt.nlcl_original_chapter_ops_usage_export_receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  chapter_relative_paths: receipt.chapter_relative_paths,
  written_files: [
    {
      relative_path: 'state/project_status.json',
      source_hash: '6'.repeat(64),
      domain: 'project_status' as const,
      created: true,
    },
    {
      relative_path: 'state/character_state.json',
      source_hash: '7'.repeat(64),
      domain: 'character' as const,
      created: true,
    },
    {
      relative_path: 'state/plot_state.json',
      source_hash: '8'.repeat(64),
      domain: 'plot' as const,
      created: true,
    },
    {
      relative_path: 'state/foreshadowing_state.json',
      source_hash: '9'.repeat(64),
      domain: 'foreshadowing' as const,
      created: true,
    },
    {
      relative_path: 'state/truth_character_matrix.json',
      source_hash: 'a'.repeat(64),
      domain: 'truth_character_matrix' as const,
      created: true,
    },
    {
      relative_path: 'state/truth_foreshadow_lifecycle.json',
      source_hash: 'b'.repeat(64),
      domain: 'truth_foreshadow_lifecycle' as const,
      created: true,
    },
    {
      relative_path: 'state/truth_info_boundary.json',
      source_hash: 'c'.repeat(64),
      domain: 'truth_info_boundary' as const,
      created: true,
    },
  ],
  state_root_relative_path: 'state',
  record_mode: 'product_bridge_original_state_json_writeback' as const,
  original_nlcl_call_usage_recorded: true as const,
  original_nlcl_chapter_ops_usage_synced: true as const,
  original_nlcl_novel_state_bridge_recorded: true as const,
  original_nlcl_novel_state_synced: true as const,
  original_nlcl_truth_matrix_synced: true as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'a'.repeat(64),
}

describe('NLCL novel-state sync API client', () => {
  it('gets and posts novel-state sync', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-novel-state-sync-response.v1',
      change_set_id: syncReceipt.change_set_id,
      receipt: syncReceipt,
      history: [syncReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclNovelStateSync(syncReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.novel_state_synced).toBe(true)
    await exportOriginalNlclNovelStateSync(
      syncReceipt.change_set_id,
      syncReceipt.nlcl_original_novel_state_bridge_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_novel_state_bridge_receipt_hash:
        syncReceipt.nlcl_original_novel_state_bridge_receipt_hash,
    })
  })
})



const prewriteReceipt = {
  schema: 'zhimeng.nlcl-original-prewrite-context-report-receipt.v1' as const,
  receipt_id: 'nlcl-original-prewrite-context-report-' + 'b'.repeat(32),
  change_set_id: syncReceipt.change_set_id,
  project_id: syncReceipt.project_id,
  nlcl_original_novel_state_sync_receipt_id: syncReceipt.receipt_id,
  nlcl_original_novel_state_sync_receipt_hash: syncReceipt.receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  written_files: [
    {
      relative_path: 'chapter_context/CH001_prewrite_context_report_latest.json',
      source_hash: '1'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_prewrite_context_report_latest.md',
      source_hash: '2'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_prewrite_context_report_post_writeback.json',
      source_hash: '3'.repeat(64),
      kind: 'json_stage' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_prewrite_context_report_post_writeback.md',
      source_hash: '4'.repeat(64),
      kind: 'md_stage' as const,
      created: true,
    },
  ],
  report_relative_path: 'chapter_context/CH001_prewrite_context_report_latest.json',
  record_mode: 'product_bridge_prewrite_context_report' as const,
  original_nlcl_novel_state_synced: true as const,
  original_nlcl_truth_matrix_synced: true as const,
  prewrite_context_report_synced: true as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'd'.repeat(64),
}

describe('NLCL prewrite context report API client', () => {
  it('gets and posts prewrite context report', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-prewrite-context-report-response.v1',
      change_set_id: prewriteReceipt.change_set_id,
      receipt: prewriteReceipt,
      history: [prewriteReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclPrewriteContextReport(prewriteReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.prewrite_context_report_synced).toBe(true)
    await exportOriginalNlclPrewriteContextReport(
      prewriteReceipt.change_set_id,
      prewriteReceipt.nlcl_original_novel_state_sync_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_novel_state_sync_receipt_hash:
        prewriteReceipt.nlcl_original_novel_state_sync_receipt_hash,
    })
  })
})

const extractReceipt = {
  schema: 'zhimeng.nlcl-original-truth-content-extract-receipt.v1' as const,
  receipt_id: 'nlcl-original-truth-content-extract-' + 'c'.repeat(32),
  change_set_id: syncReceipt.change_set_id,
  project_id: syncReceipt.project_id,
  nlcl_original_novel_state_sync_receipt_id: syncReceipt.receipt_id,
  nlcl_original_novel_state_sync_receipt_hash: syncReceipt.receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  source_files: [
    {
      relative_path: '03_人物设定/主角.md',
      source_hash: '5'.repeat(64),
      section_index: 3,
      extracted_item_count: 1,
    },
  ],
  written_files: [
    {
      relative_path: 'state/character_state.json',
      source_hash: '6'.repeat(64),
      domain: 'character' as const,
      created: false,
      item_count: 1,
    },
    {
      relative_path: 'state/plot_state.json',
      source_hash: '7'.repeat(64),
      domain: 'plot' as const,
      created: false,
      item_count: 1,
    },
    {
      relative_path: 'state/foreshadowing_state.json',
      source_hash: '8'.repeat(64),
      domain: 'foreshadowing' as const,
      created: false,
      item_count: 1,
    },
    {
      relative_path: 'state/truth_character_matrix.json',
      source_hash: '9'.repeat(64),
      domain: 'truth_character_matrix' as const,
      created: false,
      item_count: 1,
    },
    {
      relative_path: 'state/truth_foreshadow_lifecycle.json',
      source_hash: 'a'.repeat(64),
      domain: 'truth_foreshadow_lifecycle' as const,
      created: false,
      item_count: 1,
    },
    {
      relative_path: 'state/truth_info_boundary.json',
      source_hash: 'b'.repeat(64),
      domain: 'truth_info_boundary' as const,
      created: false,
      item_count: 1,
    },
  ],
  extracted_character_count: 1,
  extracted_foreshadow_count: 1,
  extracted_info_boundary_count: 1,
  record_mode: 'product_bridge_truth_content_extract_from_01_09' as const,
  original_nlcl_novel_state_synced: true as const,
  original_nlcl_truth_matrix_synced: true as const,
  truth_content_extracted: true as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'e'.repeat(64),
}

describe('NLCL truth content extract API client', () => {
  it('gets and posts truth content extract', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-truth-content-extract-response.v1',
      change_set_id: extractReceipt.change_set_id,
      receipt: extractReceipt,
      history: [extractReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclTruthContentExtract(extractReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.truth_content_extracted).toBe(true)
    expect(got.receipt?.formal_draft_allowed).toBe(false)
    await exportOriginalNlclTruthContentExtract(
      extractReceipt.change_set_id,
      extractReceipt.nlcl_original_novel_state_sync_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_novel_state_sync_receipt_hash:
        extractReceipt.nlcl_original_novel_state_sync_receipt_hash,
    })
  })
})

const planReceipt = {
  schema: 'zhimeng.nlcl-original-mandatory-usage-plan-receipt.v1' as const,
  receipt_id: 'nlcl-original-mandatory-usage-plan-' + 'd'.repeat(32),
  change_set_id: extractReceipt.change_set_id,
  project_id: extractReceipt.project_id,
  nlcl_original_truth_content_extract_receipt_id: extractReceipt.receipt_id,
  nlcl_original_truth_content_extract_receipt_hash: extractReceipt.receipt_hash,
  nlcl_original_novel_state_sync_receipt_id: extractReceipt.nlcl_original_novel_state_sync_receipt_id,
  nlcl_original_novel_state_sync_receipt_hash: extractReceipt.nlcl_original_novel_state_sync_receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  items: [
    {
      item_id: 'character:qingya',
      kind: 'character' as const,
      title: '青崖',
      source_relative_path: '03_人物设定/主角.md',
      required_action: '推进角色当前状态',
      status: 'pending' as const,
    },
  ],
  written_files: [
    {
      relative_path: 'chapter_context/CH001_mandatory_usage_plan_latest.json',
      source_hash: '1'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_mandatory_usage_plan_latest.md',
      source_hash: '2'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  plan_relative_path: 'chapter_context/CH001_mandatory_usage_plan_latest.json',
  record_mode: 'product_bridge_mandatory_usage_plan_scaffold' as const,
  original_nlcl_novel_state_synced: true as const,
  original_nlcl_truth_matrix_synced: true as const,
  truth_content_extracted: true as const,
  mandatory_usage_plan_synced: true as const,
  mandatory_usage_closure_closed: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'f'.repeat(64),
}

describe('NLCL mandatory usage plan API client', () => {
  it('gets and posts mandatory usage plan scaffold', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-mandatory-usage-plan-response.v1',
      change_set_id: planReceipt.change_set_id,
      receipt: planReceipt,
      history: [planReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclMandatoryUsagePlan(planReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.mandatory_usage_plan_synced).toBe(true)
    expect(got.receipt?.mandatory_usage_closure_closed).toBe(false)
    await exportOriginalNlclMandatoryUsagePlan(
      planReceipt.change_set_id,
      planReceipt.nlcl_original_truth_content_extract_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_truth_content_extract_receipt_hash:
        planReceipt.nlcl_original_truth_content_extract_receipt_hash,
    })
  })
})

const closureReceipt = {
  schema: 'zhimeng.nlcl-original-mandatory-usage-closure-receipt.v1' as const,
  receipt_id: 'nlcl-original-mandatory-usage-closure-' + 'e'.repeat(32),
  change_set_id: planReceipt.change_set_id,
  project_id: planReceipt.project_id,
  nlcl_original_mandatory_usage_plan_receipt_id: planReceipt.receipt_id,
  nlcl_original_mandatory_usage_plan_receipt_hash: planReceipt.receipt_hash,
  nlcl_original_truth_content_extract_receipt_id: planReceipt.nlcl_original_truth_content_extract_receipt_id,
  nlcl_original_truth_content_extract_receipt_hash: planReceipt.nlcl_original_truth_content_extract_receipt_hash,
  nlcl_original_novel_state_sync_receipt_id: planReceipt.nlcl_original_novel_state_sync_receipt_id,
  nlcl_original_novel_state_sync_receipt_hash: planReceipt.nlcl_original_novel_state_sync_receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  items: [
    {
      item_id: 'character:qingya',
      kind: 'character' as const,
      title: '青崖',
      source_relative_path: '03_人物设定/主角.md',
      required_action: '推进角色当前状态',
      status: 'used' as const,
      note: 'author decided',
    },
  ],
  written_files: [
    {
      relative_path: 'chapter_context/CH001_mandatory_usage_closure_latest.json',
      source_hash: '3'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_mandatory_usage_closure_latest.md',
      source_hash: '4'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  closure_relative_path: 'chapter_context/CH001_mandatory_usage_closure_latest.json',
  overall_status: 'closed' as const,
  record_mode: 'product_bridge_mandatory_usage_closure_author_decided' as const,
  original_nlcl_novel_state_synced: true as const,
  original_nlcl_truth_matrix_synced: true as const,
  truth_content_extracted: true as const,
  mandatory_usage_plan_synced: true as const,
  mandatory_usage_closure_closed: true as const,
  route_validated: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: '0'.repeat(64),
}

describe('NLCL mandatory usage closure API client', () => {
  it('gets and posts author-decided closure', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-mandatory-usage-closure-response.v1',
      change_set_id: closureReceipt.change_set_id,
      receipt: closureReceipt,
      history: [closureReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclMandatoryUsageClosure(closureReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.mandatory_usage_closure_closed).toBe(true)
    expect(got.receipt?.route_validated).toBe(false)
    await exportOriginalNlclMandatoryUsageClosure(
      closureReceipt.change_set_id,
      closureReceipt.nlcl_original_mandatory_usage_plan_receipt_hash,
      [{ item_id: 'character:qingya', status: 'used', note: 'author decided' }],
      true,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_mandatory_usage_plan_receipt_hash:
        closureReceipt.nlcl_original_mandatory_usage_plan_receipt_hash,
      decisions: [{ item_id: 'character:qingya', status: 'used', note: 'author decided' }],
      finalize: true,
    })
  })
})

const packageLikeReceipt = {
  schema: 'zhimeng.nlcl-original-operation-package-like-receipt.v1' as const,
  receipt_id: 'nlcl-original-operation-package-like-' + '1'.repeat(32),
  change_set_id: closureReceipt.change_set_id,
  project_id: closureReceipt.project_id,
  nlcl_original_mandatory_usage_closure_receipt_id: closureReceipt.receipt_id,
  nlcl_original_mandatory_usage_closure_receipt_hash: closureReceipt.receipt_hash,
  nlcl_original_mandatory_usage_plan_receipt_id: closureReceipt.nlcl_original_mandatory_usage_plan_receipt_id,
  nlcl_original_mandatory_usage_plan_receipt_hash: closureReceipt.nlcl_original_mandatory_usage_plan_receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  operation_package_id: 'product-bridge-operation-package-' + '2'.repeat(32),
  package_hash: '3'.repeat(64),
  readiness: 'risk_draft_only' as const,
  written_files: [
    {
      relative_path: 'chapter_context/CH001_operation_package_like_latest.json',
      source_hash: '4'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_operation_package_like_latest.md',
      source_hash: '5'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  package_relative_path: 'chapter_context/CH001_operation_package_like_latest.json',
  record_mode: 'product_bridge_operation_package_like_scaffold' as const,
  original_nlcl_novel_state_synced: true as const,
  truth_content_extracted: true as const,
  mandatory_usage_plan_synced: true as const,
  mandatory_usage_closure_closed: true as const,
  operation_package_like_synced: true as const,
  route_validated: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: '6'.repeat(64),
}

describe('NLCL operation_package_like API client', () => {
  it('gets and posts operation package like scaffold', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-operation-package-like-response.v1',
      change_set_id: packageLikeReceipt.change_set_id,
      receipt: packageLikeReceipt,
      history: [packageLikeReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclOperationPackageLike(packageLikeReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.operation_package_like_synced).toBe(true)
    expect(got.receipt?.readiness).toBe('risk_draft_only')
    await exportOriginalNlclOperationPackageLike(
      packageLikeReceipt.change_set_id,
      packageLikeReceipt.nlcl_original_mandatory_usage_closure_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_mandatory_usage_closure_receipt_hash:
        packageLikeReceipt.nlcl_original_mandatory_usage_closure_receipt_hash,
    })
  })
})

const layoutReceipt = {
  schema: 'zhimeng.nlcl-original-library-layout-inventory-receipt.v1' as const,
  receipt_id: 'nlcl-original-library-layout-inventory-' + '7'.repeat(32),
  change_set_id: packageLikeReceipt.change_set_id,
  project_id: packageLikeReceipt.project_id,
  nlcl_original_operation_package_like_receipt_id: packageLikeReceipt.receipt_id,
  nlcl_original_operation_package_like_receipt_hash: packageLikeReceipt.receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  slots: [
    {
      slot_id: 'section_03',
      kind: 'section_03' as const,
      label: '人物设定',
      relative_path: '03_人物设定',
      present: true,
      file_count: 1,
      note: '',
    },
  ],
  present_slot_count: 1,
  missing_slot_count: 0,
  written_files: [
    {
      relative_path: 'chapter_context/CH001_library_layout_inventory_latest.json',
      source_hash: '8'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_library_layout_inventory_latest.md',
      source_hash: '9'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  inventory_relative_path: 'chapter_context/CH001_library_layout_inventory_latest.json',
  record_mode: 'product_bridge_library_layout_inventory' as const,
  operation_package_like_synced: true as const,
  library_layout_inventoried: true as const,
  library_layout_complete: false as const,
  whole_library_load_forbidden: true as const,
  route_validated: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'a'.repeat(64),
}

describe('NLCL library layout inventory API client', () => {
  it('gets and posts library layout inventory', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-library-layout-inventory-response.v1',
      change_set_id: layoutReceipt.change_set_id,
      receipt: layoutReceipt,
      history: [layoutReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclLibraryLayoutInventory(layoutReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.library_layout_inventoried).toBe(true)
    expect(got.receipt?.library_layout_complete).toBe(false)
    await exportOriginalNlclLibraryLayoutInventory(
      layoutReceipt.change_set_id,
      layoutReceipt.nlcl_original_operation_package_like_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_operation_package_like_receipt_hash:
        layoutReceipt.nlcl_original_operation_package_like_receipt_hash,
    })
  })
})

const completenessReceipt = {
  schema: 'zhimeng.nlcl-original-library-layout-completeness-receipt.v1' as const,
  receipt_id: 'nlcl-original-library-layout-completeness-' + '8'.repeat(32),
  change_set_id: layoutReceipt.change_set_id,
  project_id: layoutReceipt.project_id,
  nlcl_original_library_layout_inventory_receipt_id: layoutReceipt.receipt_id,
  nlcl_original_library_layout_inventory_receipt_hash: layoutReceipt.receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  slots: layoutReceipt.slots,
  required_slot_count: 1,
  present_required_slot_count: 1,
  missing_required_slot_count: 0,
  completeness_status: 'complete' as const,
  written_files: [
    {
      relative_path: 'chapter_context/CH001_library_layout_completeness_latest.json',
      source_hash: 'b'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_library_layout_completeness_latest.md',
      source_hash: 'c'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  assessment_relative_path: 'chapter_context/CH001_library_layout_completeness_latest.json',
  record_mode: 'product_bridge_library_layout_completeness' as const,
  library_layout_inventoried: true as const,
  library_layout_assessed: true as const,
  library_layout_complete: true,
  whole_library_load_forbidden: true as const,
  route_validated: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: 'd'.repeat(64),
}

describe('NLCL library layout completeness API client', () => {
  it('gets and posts library layout completeness', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-library-layout-completeness-response.v1',
      change_set_id: completenessReceipt.change_set_id,
      receipt: completenessReceipt,
      history: [completenessReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclLibraryLayoutCompleteness(completenessReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.library_layout_assessed).toBe(true)
    expect(got.receipt?.library_layout_complete).toBe(true)
    expect(got.receipt?.whole_library_load_forbidden).toBe(true)
    await exportOriginalNlclLibraryLayoutCompleteness(
      completenessReceipt.change_set_id,
      completenessReceipt.nlcl_original_library_layout_inventory_receipt_hash,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_library_layout_inventory_receipt_hash:
        completenessReceipt.nlcl_original_library_layout_inventory_receipt_hash,
    })
  })
})

const activationReceipt = {
  schema: 'zhimeng.nlcl-original-bounded-library-activation-receipt.v1' as const,
  receipt_id: 'nlcl-original-bounded-library-activation-' + '9'.repeat(32),
  change_set_id: completenessReceipt.change_set_id,
  project_id: completenessReceipt.project_id,
  nlcl_original_library_layout_completeness_receipt_id: completenessReceipt.receipt_id,
  nlcl_original_library_layout_completeness_receipt_hash: completenessReceipt.receipt_hash,
  outcome: 'applied' as const,
  chapter_id: 'CH001',
  canonical_chapter_key: 'chapter:1',
  activated_sources: [
    {
      source_id: 'state/project_status.json',
      slot_id: 'state:project_status.json',
      kind: 'state_file' as const,
      relative_path: 'state/project_status.json',
      character_count: 120,
      token_estimate: 30,
    },
  ],
  activated_source_ids: ['state/project_status.json'],
  max_sources: 8,
  activated_count: 1,
  candidate_present_count: 1,
  max_characters: 120000,
  used_characters: 120,
  max_tokens: 30000,
  used_tokens: 30,
  written_files: [
    {
      relative_path: 'chapter_context/CH001_bounded_library_activation_latest.json',
      source_hash: 'e'.repeat(64),
      kind: 'json_latest' as const,
      created: true,
    },
    {
      relative_path: 'chapter_context/CH001_bounded_library_activation_latest.md',
      source_hash: 'f'.repeat(64),
      kind: 'md_latest' as const,
      created: true,
    },
  ],
  activation_relative_path: 'chapter_context/CH001_bounded_library_activation_latest.json',
  record_mode: 'product_bridge_bounded_library_activation' as const,
  library_layout_inventoried: true as const,
  library_layout_assessed: true as const,
  library_layout_complete: true,
  bounded_library_activated: true as const,
  whole_library_load: false as const,
  whole_library_load_forbidden: true as const,
  route_validated: false as const,
  formal_draft_allowed: false as const,
  quality_assessed: false as const,
  novel_state_synced: true as const,
  created_at: '2026-07-26T00:00:00+00:00',
  receipt_hash: '1'.repeat(64),
}

describe('NLCL bounded library activation API client', () => {
  it('gets and posts bounded library activation', async () => {
    const body = {
      schema: 'zhimeng.nlcl-original-bounded-library-activation-response.v1',
      change_set_id: activationReceipt.change_set_id,
      receipt: activationReceipt,
      history: [activationReceipt],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(body))
    const got = await getOriginalNlclBoundedLibraryActivation(activationReceipt.change_set_id, {
      fetcher,
      baseUrl: 'http://127.0.0.1:8787',
    })
    expect(got.receipt?.bounded_library_activated).toBe(true)
    expect(got.receipt?.whole_library_load).toBe(false)
    await exportOriginalNlclBoundedLibraryActivation(
      activationReceipt.change_set_id,
      activationReceipt.nlcl_original_library_layout_completeness_receipt_hash,
      8,
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(JSON.parse(String(fetcher.mock.calls[1]?.[1]?.body))).toEqual({
      expected_nlcl_original_library_layout_completeness_receipt_hash:
        activationReceipt.nlcl_original_library_layout_completeness_receipt_hash,
      max_sources: 8,
    })
  })
})


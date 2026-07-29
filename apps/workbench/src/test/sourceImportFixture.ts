import type {
  DistillationDraft,
  LocalFileSourceCandidate,
  LocalSourceImportJob,
  PreparedSourcePacket,
  SourceImportExecutionReceiptRecord,
  SourceImportJob,
  SourceImportStatus,
  TomatoSearchCandidate,
} from '../api/sourceImports'
import type {
  KnowledgeFormalizationRecord,
  KnowledgeFormalizationStatus,
} from '../api/knowledge'

export const tomatoCandidateFixture: TomatoSearchCandidate = {
  index: 1,
  book_id: '1234567890',
  title: '长夜巡灯人',
  author: '闻舟',
}

export const localSourceCandidateFixture: LocalFileSourceCandidate = {
  index: 1,
  source_path: 'C:\\NovelSources\\sample-novel.txt',
  source_format: 'txt',
  title: '本地样例长篇',
  author: 'unknown',
  source_size_bytes: 12_480,
  source_sha256: '3'.repeat(64),
  source_locator_hash: '4'.repeat(64),
}

export const localEpubSourceCandidateFixture: LocalFileSourceCandidate = {
  ...localSourceCandidateFixture,
  source_path: 'C:\\NovelSources\\sample-novel.epub',
  source_format: 'epub',
  title: '本地 EPUB 样例',
  source_size_bytes: 495_983,
  source_sha256: '5'.repeat(64),
  source_locator_hash: '6'.repeat(64),
}

const sourcePacketFixture: PreparedSourcePacket = {
  title: tomatoCandidateFixture.title,
  author: tomatoCandidateFixture.author,
  download_scope: 'full',
  packet_dir: 'C:\\Users\\30865\\Desktop\\番茄下载\\source-library\\raw\\packet',
  packet_manifest_path: 'C:\\packet\\packet.manifest.json',
  epub_path: 'C:\\packet\\book.epub',
  downloaded_epub_path: 'C:\\Users\\30865\\Desktop\\番茄下载\\长夜巡灯人.epub',
  text_path: 'C:\\packet\\book.txt',
  metadata_path: 'C:\\packet\\book.metadata.json',
  char_count: 856_240,
  chapter_marker_count: 412,
  artifacts: [
    { kind: 'packet_manifest', path: 'C:\\packet\\packet.manifest.json', size_bytes: 120, sha256: 'a'.repeat(64) },
    { kind: 'packet_epub', path: 'C:\\packet\\book.epub', size_bytes: 500, sha256: 'b'.repeat(64) },
    { kind: 'packet_text', path: 'C:\\packet\\book.txt', size_bytes: 900, sha256: 'c'.repeat(64) },
    { kind: 'packet_metadata', path: 'C:\\packet\\book.metadata.json', size_bytes: 80, sha256: 'd'.repeat(64) },
  ],
}

const distillationDraftFixture: DistillationDraft = {
  status: 'draft_ready',
  output_dir: 'C:\\draft',
  cards_path: 'C:\\draft\\cards.draft.json',
  report_path: 'C:\\draft\\distillation_report.md',
  validation_report_path: 'C:\\draft\\validation_report.md',
  envelope_path: 'C:\\draft\\tool_result_envelope.json',
  card_count: 8,
  requires_review: true,
  artifacts: [
    { kind: 'cards_draft_json', path: 'C:\\draft\\cards.draft.json', size_bytes: 500, sha256: 'e'.repeat(64) },
    { kind: 'distillation_report_md', path: 'C:\\draft\\distillation_report.md', size_bytes: 300, sha256: 'f'.repeat(64) },
    { kind: 'validation_report_md', path: 'C:\\draft\\validation_report.md', size_bytes: 200, sha256: '1'.repeat(64) },
    { kind: 'tool_result_envelope_json', path: 'C:\\draft\\tool_result_envelope.json', size_bytes: 150, sha256: '2'.repeat(64) },
  ],
}

export function sourceImportJobFixture(options: {
  importId?: string
  status?: SourceImportStatus
  candidate?: TomatoSearchCandidate
  percent?: number | null
  savedChapters?: number | null
  totalChapters?: number | null
  error?: string | null
  retryOf?: string | null
} = {}): SourceImportJob {
  const status = options.status ?? 'queued'
  const hasSource = ['source_ready', 'drafting', 'awaiting_review', 'formalizing', 'completed'].includes(status)
  const hasDraft = ['awaiting_review', 'formalizing', 'completed'].includes(status)
  return {
    schema: 'zhimeng.source-import.v1',
    import_id: options.importId ?? 'source-import-0123456789abcdef0123456789abcdef',
    provider: 'tomato',
    query: options.candidate?.title ?? tomatoCandidateFixture.title,
    candidate: options.candidate ?? tomatoCandidateFixture,
    status,
    retry_of: options.retryOf ?? null,
    progress: {
      stage: status,
      message: status === 'awaiting_review' ? 'Distillation draft is ready for review' : `Fixture ${status}`,
      saved_chapters: options.savedChapters ?? null,
      total_chapters: options.totalChapters ?? null,
      percent: options.percent ?? null,
    },
    source_packet: hasSource ? sourcePacketFixture : null,
    distillation_draft: hasDraft ? distillationDraftFixture : null,
    error: options.error ?? null,
    created_at: '2026-07-19T02:00:00Z',
    updated_at: '2026-07-19T02:05:00Z',
  }
}

export function localSourceImportJobFixture(options: {
  importId?: string
  status?: SourceImportStatus
  candidate?: LocalFileSourceCandidate
  error?: string | null
  retryOf?: string | null
} = {}): LocalSourceImportJob {
  const status = options.status ?? 'queued'
  const candidate = options.candidate ?? localSourceCandidateFixture
  return {
    schema: 'zhimeng.source-import.v2',
    import_id: options.importId ?? 'source-import-abcdef0123456789abcdef0123456789',
    provider: 'local_file',
    query: candidate.source_path,
    candidate,
    status,
    retry_of: options.retryOf ?? null,
    progress: {
      stage: status,
      message: `Fixture local ${status}`,
      saved_chapters: null,
      total_chapters: null,
      percent: null,
    },
    source_packet: null,
    distillation_draft: null,
    error: options.error ?? null,
    created_at: '2026-07-22T15:00:00Z',
    updated_at: '2026-07-22T15:01:00Z',
  }
}

function fixtureIdSuffix(importId: string): string {
  const suffix = importId.replace(/^source-import-/, '')
  return /^[0-9a-f]{32}$/.test(suffix) ? suffix : '0'.repeat(32)
}

export function sourceImportReceiptFixture(options: {
  importId?: string
  lineageStatus?: 'verified' | 'legacy_unverified'
  distillationInvocationId?: string
} = {}): SourceImportExecutionReceiptRecord {
  const importId = options.importId ?? sourceImportJobFixture().import_id
  const lineageStatus = options.lineageStatus ?? 'verified'
  if (lineageStatus === 'legacy_unverified') {
    return {
      schema: 'zhimeng.source-import-execution-receipt-record.v1',
      source_import_id: importId,
      lineage_status: 'legacy_unverified',
      receipt: null,
    }
  }
  const suffix = fixtureIdSuffix(importId)
  return {
    schema: 'zhimeng.source-import-execution-receipt-record.v1',
    source_import_id: importId,
    lineage_status: 'verified',
    receipt: {
      schema: 'zhimeng.source-import-execution-receipt.v1',
      receipt_id: `source-import-receipt-${suffix}`,
      source_import_id: importId,
      run_id: `source-import-run-${suffix}`,
      revision: 1,
      idempotency_key: `source-import-idempotency-${suffix}`,
      lineage_status: 'verified',
      provider: 'tomato',
      request: {
        query: tomatoCandidateFixture.title,
        candidate_index: tomatoCandidateFixture.index,
        book_id: tomatoCandidateFixture.book_id,
        title: tomatoCandidateFixture.title,
        author: tomatoCandidateFixture.author,
      },
      request_hash: '1'.repeat(64),
      retry_of: null,
      prepare_invocation_id: `source-import-invocation-${'1'.repeat(32)}`,
      prepare_tool: {
        tool_id: 'tomato.prepare',
        tool_version: '1',
        tool_fingerprint: '2'.repeat(64),
      },
      prepare_input_hash: '3'.repeat(64),
      prepare_output_hash: '4'.repeat(64),
      download_scope: 'full',
      source_packet_fingerprint: '5'.repeat(64),
      packet_manifest_hash: 'a'.repeat(64),
      packet_text_hash: 'c'.repeat(64),
      packet_metadata_hash: 'd'.repeat(64),
      source_artifacts: [
        {
          kind: 'downloaded_epub',
          relative_path: 'source/downloaded_epub',
          size_bytes: 500,
          sha256: 'b'.repeat(64),
        },
        ...sourcePacketFixture.artifacts.map((artifact) => ({
          kind: artifact.kind,
          relative_path: `source/${artifact.kind}`,
          size_bytes: artifact.size_bytes ?? 0,
          sha256: artifact.sha256 ?? '0'.repeat(64),
        })),
      ].sort((left, right) => left.kind < right.kind ? -1 : left.kind > right.kind ? 1 : 0),
      distillation_invocation_id: options.distillationInvocationId ?? `source-import-invocation-${'2'.repeat(32)}`,
      distillation_tool: {
        tool_id: 'novel-distillation.run',
        tool_version: '1',
        tool_fingerprint: '6'.repeat(64),
      },
      distillation_input_hash: '7'.repeat(64),
      distillation_output_hash: '8'.repeat(64),
      draft_fingerprint: '9'.repeat(64),
      cards_draft_hash: 'e'.repeat(64),
      envelope_hash: '2'.repeat(64),
      envelope_schema: 'novel_distillation.source_packet_draft.v1',
      envelope_status: 'draft_ready',
      card_count: distillationDraftFixture.card_count,
      draft_artifacts: distillationDraftFixture.artifacts
        .map((artifact) => ({
          kind: artifact.kind,
          relative_path: `draft/${artifact.kind}`,
          size_bytes: artifact.size_bytes ?? 0,
          sha256: artifact.sha256 ?? '0'.repeat(64),
        }))
        .sort((left, right) => left.kind < right.kind ? -1 : left.kind > right.kind ? 1 : 0),
      created_at: '2026-07-19T02:05:00Z',
      receipt_hash: 'f'.repeat(64),
    },
  }
}

export function sourceImportFormalizationFixture(options: {
  importId?: string
  status?: KnowledgeFormalizationStatus
} = {}): KnowledgeFormalizationRecord {
  const importId = options.importId ?? sourceImportJobFixture().import_id
  const status = options.status ?? 'awaiting_review'
  const suffix = fixtureIdSuffix(importId)
  return {
    schema: 'zhimeng.knowledge-formalization-record.v1',
    source_import_id: importId,
    status,
    receipt: status === 'verified' ? {
      schema: 'zhimeng.knowledge-formalization-receipt.v1',
      formalization_receipt_id: `knowledge-formalization-${suffix}`,
      source_import_id: importId,
      source_receipt_id: `source-import-receipt-${suffix}`,
      source_receipt_hash: 'f'.repeat(64),
      source_packet_fingerprint: '5'.repeat(64),
      draft_fingerprint: '9'.repeat(64),
      cards_draft_hash: 'e'.repeat(64),
      candidate_set_hash: 'a'.repeat(64),
      decisions: [{
        candidate_id: `knowledge-candidate-${suffix}`,
        source_card_index: 0,
        origin_hash: 'b'.repeat(64),
        decision: 'promoted',
        revision: 1,
        content_hash: 'c'.repeat(64),
        promoted_version_id: `knowledge-version-${suffix}`,
        version_lineage_receipt_id: `knowledge-lineage-${suffix}`,
        version_lineage_receipt_hash: 'd'.repeat(64),
      }],
      knowledge_state_hash: 'e'.repeat(64),
      created_at: '2026-07-19T02:10:00Z',
      receipt_hash: '0'.repeat(64),
    } : null,
  }
}

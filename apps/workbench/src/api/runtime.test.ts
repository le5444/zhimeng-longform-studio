import { describe, expect, it, vi } from 'vitest'
import { createRuntimeBackup, evaluateReleaseReadiness, exportDiagnosticsPackage, fetchRuntimeSnapshot, getReleaseReadiness, listRuntimeBackups, restoreRuntimeBackup } from './runtime'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('fetchRuntimeSnapshot', () => {
  it('merges readiness and runtime status payloads', async () => {
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse({ status: 'ready', version: '0.1.0' }))
      .mockResolvedValueOnce(
        jsonResponse({ status: 'idle', current_run: { status: 'paused' } }),
      )

    const snapshot = await fetchRuntimeSnapshot({ fetcher, baseUrl: 'http://127.0.0.1:7341' })

    expect(snapshot.connection).toBe('online')
    expect(snapshot.ready).toBe(true)
    expect(snapshot.runStatus).toBe('paused')
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:7341/health/ready',
      expect.any(Object),
    )
  })

  it('returns an explicit offline state when both endpoints fail', async () => {
    const fetcher = vi.fn<typeof fetch>().mockRejectedValue(new TypeError('offline'))
    const snapshot = await fetchRuntimeSnapshot({ fetcher })

    expect(snapshot.connection).toBe('offline')
    expect(snapshot.ready).toBe(false)
    expect(snapshot.detail).toContain('未连接')
  })
})

describe('runtime diagnostics and backup clients', () => {
  it('exports a redacted diagnostics package', async () => {
    const fixture = {
      schema: 'zhimeng.diagnostics-package-response.v1',
      receipt: {
        schema: 'zhimeng.diagnostics-package-receipt.v1',
        receipt_id: 'diagnostics-package-' + 'a'.repeat(32),
        export_relative_path: 'diagnostics/diagnostics-package-aaaaaaaa.json',
        formal_draft_allowed: false,
        writes_project_truth: false,
        contains_secrets: false,
        contains_novel_body: false,
        provider_api_key_present: true,
        provider_api_key_redacted: true,
        integrity_check: 'ok',
        created_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: 'b'.repeat(64),
      },
      package: { config_redacted: { provider_api_key: '[redacted]' } },
      not_claiming: ['local_v1_exe'],
    }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(fixture))
    const result = await exportDiagnosticsPackage({ fetcher, baseUrl: 'http://127.0.0.1:7341' })
    expect(result.receipt.contains_secrets).toBe(false)
    expect(result.receipt.formal_draft_allowed).toBe(false)
    expect(fetcher).toHaveBeenCalledWith(
      'http://127.0.0.1:7341/api/v1/runtime/diagnostics-package',
      expect.objectContaining({ method: 'POST' }),
    )
  })

  it('creates lists and restores runtime backups', async () => {
    const createFixture = {
      schema: 'zhimeng.runtime-backup-response.v1',
      receipt: {
        schema: 'zhimeng.runtime-backup-receipt.v1',
        receipt_id: 'runtime-backup-receipt-' + 'c'.repeat(32),
        backup_id: 'runtime-backup-' + 'd'.repeat(32),
        outcome: 'created',
        backup_relative_path: 'backups/runtime-backup-' + 'd'.repeat(32),
        manifest: {
          schema: 'zhimeng.runtime-backup-manifest.v1',
          backup_id: 'runtime-backup-' + 'd'.repeat(32),
          created_at: '2026-07-26T00:00:00+00:00',
          product_version: '0.1.0',
          environment: 'test',
          database_schema_version: 23,
          journal_mode: 'wal',
          database_relative_path: 'writing-runtime.sqlite3',
          database_sha256: 'e'.repeat(64),
          database_size_bytes: 1024,
          integrity_check: 'ok',
          foreign_key_violations: 0,
          includes_secrets: false,
          includes_novel_body: false,
          includes_project_files: false,
          formal_draft_allowed: false,
          policy_version: 'local-v1-runtime-backup-1',
          manifest_hash: 'f'.repeat(64),
        },
        formal_draft_allowed: false,
        writes_project_truth: false,
        created_at: '2026-07-26T00:00:00+00:00',
        receipt_hash: '1'.repeat(64),
      },
      not_claiming: ['project_file_backup'],
    }
    const listFixture = {
      schema: 'zhimeng.runtime-backup-list.v1',
      backups: [{
        backup_id: createFixture.receipt.backup_id,
        created_at: createFixture.receipt.created_at,
        database_schema_version: 23,
        database_sha256: 'e'.repeat(64),
        database_size_bytes: 1024,
        integrity_check: 'ok',
        manifest_hash: 'f'.repeat(64),
        backup_relative_path: createFixture.receipt.backup_relative_path,
      }],
      not_claiming: ['project_file_backup'],
    }
    const restoreFixture = {
      schema: 'zhimeng.runtime-restore-response.v1',
      receipt: {
        schema: 'zhimeng.runtime-restore-receipt.v1',
        receipt_id: 'runtime-restore-receipt-' + '2'.repeat(32),
        backup_id: createFixture.receipt.backup_id,
        outcome: 'restored',
        restored_database_schema_version: 23,
        restored_database_sha256: 'e'.repeat(64),
        integrity_check: 'ok',
        foreign_key_violations: 0,
        pre_restore_backup_id: 'runtime-backup-' + '3'.repeat(32),
        formal_draft_allowed: false,
        writes_project_truth: false,
        includes_project_files: false,
        created_at: '2026-07-26T00:01:00+00:00',
        receipt_hash: '4'.repeat(64),
      },
      not_claiming: ['project_file_restore'],
    }
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(createFixture))
      .mockResolvedValueOnce(jsonResponse(listFixture))
      .mockResolvedValueOnce(jsonResponse(restoreFixture))

    const created = await createRuntimeBackup({ fetcher, baseUrl: 'http://127.0.0.1:7341' })
    const listed = await listRuntimeBackups({ fetcher, baseUrl: 'http://127.0.0.1:7341' })
    const restored = await restoreRuntimeBackup(
      {
        backup_id: created.receipt.backup_id,
        expected_manifest_hash: created.receipt.manifest.manifest_hash,
      },
      { fetcher, baseUrl: 'http://127.0.0.1:7341' },
    )

    expect(created.receipt.formal_draft_allowed).toBe(false)
    expect(listed.backups).toHaveLength(1)
    expect(restored.receipt.outcome).toBe('restored')
    expect(fetcher).toHaveBeenNthCalledWith(
      1,
      'http://127.0.0.1:7341/api/v1/runtime/backups',
      expect.objectContaining({ method: 'POST' }),
    )
    expect(fetcher).toHaveBeenNthCalledWith(
      3,
      'http://127.0.0.1:7341/api/v1/runtime/backups/restore',
      expect.objectContaining({ method: 'POST' }),
    )
  })
})

describe('runtime release readiness client', () => {
  it('loads and evaluates release readiness fail-closed', async () => {
    const fixture = {
      schema: 'zhimeng.local-v1-release-readiness-response.v1' as const,
      receipt: {
        schema: 'zhimeng.local-v1-release-readiness.v1' as const,
        status: 'not_ready' as const,
        local_v1_ready: false,
        local_v1_exe: false,
        formal_draft_allowed: false as const,
        quality_assessed: false as const,
        checks: [],
        missing_required: ['signed_package_ready'],
        policy_version: 'local-v1-release-readiness-1',
        generated_at: '2026-07-26T00:00:00+00:00',
        receipt_id: 'local-v1-readiness-' + 'a'.repeat(32),
        receipt_hash: 'b'.repeat(64),
      },
      not_claiming: ['local_v1_exe', 'local_v1_complete'],
    }
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture), { status: 200, headers: { 'Content-Type': 'application/json' } }))
      .mockResolvedValueOnce(new Response(JSON.stringify(fixture), { status: 200, headers: { 'Content-Type': 'application/json' } }))
    const latest = await getReleaseReadiness({ fetcher, baseUrl: 'http://127.0.0.1:7341' })
    const evaluated = await evaluateReleaseReadiness({ fetcher, baseUrl: 'http://127.0.0.1:7341' })
    expect(latest.receipt.local_v1_ready).toBe(false)
    expect(evaluated.receipt.local_v1_exe).toBe(false)
    expect(fetcher).toHaveBeenNthCalledWith(1, 'http://127.0.0.1:7341/api/v1/runtime/release-readiness', expect.any(Object))
    expect(fetcher).toHaveBeenNthCalledWith(2, 'http://127.0.0.1:7341/api/v1/runtime/release-readiness/evaluate', expect.objectContaining({ method: 'POST' }))
  })
})

import { cleanup, render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { RuntimeOpsPanel } from './RuntimeOpsPanel'

const listRuntimeBackups = vi.fn()
const createRuntimeBackup = vi.fn()
const exportDiagnosticsPackage = vi.fn()
const restoreRuntimeBackup = vi.fn()
const getReleaseReadiness = vi.fn()
const evaluateReleaseReadiness = vi.fn()

vi.mock('../api/runtime', async () => {
  const actual = await vi.importActual<typeof import('../api/runtime')>('../api/runtime')
  return {
    ...actual,
    listRuntimeBackups: (...args: unknown[]) => listRuntimeBackups(...args),
    createRuntimeBackup: (...args: unknown[]) => createRuntimeBackup(...args),
    exportDiagnosticsPackage: (...args: unknown[]) => exportDiagnosticsPackage(...args),
    restoreRuntimeBackup: (...args: unknown[]) => restoreRuntimeBackup(...args),
    getReleaseReadiness: (...args: unknown[]) => getReleaseReadiness(...args),
    evaluateReleaseReadiness: (...args: unknown[]) => evaluateReleaseReadiness(...args),
  }
})

const readinessFixture = {
  schema: 'zhimeng.local-v1-release-readiness-response.v1' as const,
  receipt: {
    schema: 'zhimeng.local-v1-release-readiness.v1' as const,
    status: 'not_ready' as const,
    local_v1_ready: false,
    local_v1_exe: false,
    formal_draft_allowed: false as const,
    quality_assessed: false as const,
    checks: [
      {
        code: 'signed_package_ready',
        required: true,
        present: false,
        detail: 'Authenticode/MSI signature readiness',
      },
      {
        code: 'all_local_v1_fr_verified',
        required: true,
        present: false,
        detail: 'all Local v1 FR AC verified_current',
      },
    ],
    missing_required: ['signed_package_ready', 'all_local_v1_fr_verified'],
    policy_version: 'local-v1-release-readiness-1',
    generated_at: '2026-07-26T00:00:00+00:00',
    receipt_id: 'local-v1-readiness-' + 'a'.repeat(32),
    receipt_hash: 'b'.repeat(64),
  },
  not_claiming: ['local_v1_exe', 'local_v1_complete'],
}

describe('RuntimeOpsPanel', () => {
  afterEach(() => {
    cleanup()
  })

  beforeEach(() => {
    listRuntimeBackups.mockReset()
    createRuntimeBackup.mockReset()
    exportDiagnosticsPackage.mockReset()
    restoreRuntimeBackup.mockReset()
    getReleaseReadiness.mockReset()
    evaluateReleaseReadiness.mockReset()
    listRuntimeBackups.mockResolvedValue({
      schema: 'zhimeng.runtime-backup-list.v1',
      backups: [],
      not_claiming: ['project_file_backup'],
    })
    getReleaseReadiness.mockResolvedValue(readinessFixture)
  })

  it('exports diagnostics and creates a backup', async () => {
    const user = userEvent.setup()
    exportDiagnosticsPackage.mockResolvedValue({
      schema: 'zhimeng.diagnostics-package-response.v1',
      receipt: {
        schema: 'zhimeng.diagnostics-package-receipt.v1',
        receipt_id: 'diagnostics-package-' + 'a'.repeat(32),
        export_relative_path: 'diagnostics/package.json',
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
      package: {},
      not_claiming: ['local_v1_exe'],
    })
    createRuntimeBackup.mockResolvedValue({
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
          database_size_bytes: 2048,
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
    })
    listRuntimeBackups
      .mockResolvedValueOnce({
        schema: 'zhimeng.runtime-backup-list.v1',
        backups: [],
        not_claiming: ['project_file_backup'],
      })
      .mockResolvedValue({
        schema: 'zhimeng.runtime-backup-list.v1',
        backups: [{
          backup_id: 'runtime-backup-' + 'd'.repeat(32),
          created_at: '2026-07-26T00:00:00+00:00',
          database_schema_version: 23,
          database_sha256: 'e'.repeat(64),
          database_size_bytes: 2048,
          integrity_check: 'ok',
          manifest_hash: 'f'.repeat(64),
          backup_relative_path: 'backups/runtime-backup-' + 'd'.repeat(32),
        }],
        not_claiming: ['project_file_backup'],
      })

    render(<RuntimeOpsPanel enabled />)

    await waitFor(() => expect(listRuntimeBackups).toHaveBeenCalled())
    expect(await screen.findByText(/Local v1 未就绪/)).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: '导出诊断包' }))
    await waitFor(() => expect(exportDiagnosticsPackage).toHaveBeenCalled())
    expect(await screen.findByText(/诊断包已导出/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '创建备份' }))
    await waitFor(() => expect(createRuntimeBackup).toHaveBeenCalled())
    expect(await screen.findByText(/已创建备份/)).toBeInTheDocument()
    expect(await screen.findByTitle('runtime-backup-' + 'd'.repeat(32))).toBeInTheDocument()
  })

  it('evaluates release readiness fail-closed', async () => {
    const user = userEvent.setup()
    evaluateReleaseReadiness.mockResolvedValue(readinessFixture)
    render(<RuntimeOpsPanel enabled />)
    await waitFor(() => expect(getReleaseReadiness).toHaveBeenCalled())
    await user.click(screen.getAllByRole('button', { name: '评估发布就绪' })[0])
    await waitFor(() => expect(evaluateReleaseReadiness).toHaveBeenCalled())
    expect(await screen.findByText(/发布就绪 fail-closed/)).toBeInTheDocument()
    expect(screen.getByText(/签名安装包/)).toBeInTheDocument()
    expect(screen.getByText(/local_v1_ready=false/)).toBeInTheDocument()
  })
})

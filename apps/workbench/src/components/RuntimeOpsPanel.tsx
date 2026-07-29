import {
  AlertTriangle,
  Archive,
  CheckCircle2,
  Download,
  HardDriveDownload,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldAlert,
  ShieldCheck,
} from 'lucide-react'
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  createRuntimeBackup,
  evaluateReleaseReadiness,
  exportDiagnosticsPackage,
  getReleaseReadiness,
  listRuntimeBackups,
  restoreRuntimeBackup,
  RuntimeApiError,
  type ReleaseReadinessResponse,
  type RuntimeBackupSummary,
} from '../api/runtime'

function humanRuntimeError(error: unknown): string {
  if (error instanceof RuntimeApiError) {
    const payload = error.payload as {
      error?: { message?: string; code?: string }
      detail?: string
    } | null
    return payload?.error?.message
      ?? payload?.detail
      ?? payload?.error?.code
      ?? `Runtime HTTP ${error.status}`
  }
  return error instanceof Error ? error.message : '未知错误'
}

function shortId(value: string): string {
  if (value.length <= 22) return value
  return `${value.slice(0, 18)}…${value.slice(-4)}`
}

const CHECK_LABELS: Record<string, string> = {
  signed_package_ready: '签名安装包',
  clean_vm_install_matrix: '干净 VM 矩阵',
  all_local_v1_fr_verified: '全部 FR verified',
  all_local_v1_nfr_measured: '全部 NFR measured',
  ace2e_001_to_007_verified: 'AC-E2E-001..007',
  local_v1_exe_claim_locked: 'EXE 宣称锁定',
}

export function RuntimeOpsPanel({ enabled }: { enabled: boolean }) {
  const [backups, setBackups] = useState<RuntimeBackupSummary[]>([])
  const [readiness, setReadiness] = useState<ReleaseReadinessResponse | null>(null)
  const [busyAction, setBusyAction] = useState<'list' | 'create' | 'diagnostics' | 'readiness' | string | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  const refresh = useCallback(async () => {
    if (!enabled) return
    setBusyAction('list')
    setError(null)
    try {
      const [backupResponse, readinessResponse] = await Promise.all([
        listRuntimeBackups(),
        getReleaseReadiness().catch(() => null),
      ])
      setBackups(backupResponse.backups)
      if (readinessResponse) setReadiness(readinessResponse)
    } catch (currentError) {
      setBackups([])
      setError(humanRuntimeError(currentError))
    } finally {
      setBusyAction(null)
    }
  }, [enabled])

  useEffect(() => {
    void refresh()
  }, [refresh])

  const runCreate = useCallback(async () => {
    if (!enabled) return
    setBusyAction('create')
    setError(null)
    setNotice(null)
    try {
      const response = await createRuntimeBackup()
      setNotice(`已创建备份 ${shortId(response.receipt.backup_id)} · formal_draft_allowed=false`)
      await refresh()
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusyAction(null)
    }
  }, [enabled, refresh])

  const runDiagnostics = useCallback(async () => {
    if (!enabled) return
    setBusyAction('diagnostics')
    setError(null)
    setNotice(null)
    try {
      const response = await exportDiagnosticsPackage()
      setNotice(
        `诊断包已导出 ${response.receipt.export_relative_path} · secrets=${String(response.receipt.contains_secrets)}`,
      )
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusyAction(null)
    }
  }, [enabled])

  const runReadiness = useCallback(async () => {
    if (!enabled) return
    setBusyAction('readiness')
    setError(null)
    setNotice(null)
    try {
      const response = await evaluateReleaseReadiness()
      setReadiness(response)
      setNotice(
        response.receipt.local_v1_ready
          ? `发布就绪评估通过 · local_v1_exe=${String(response.receipt.local_v1_exe)}`
          : `发布就绪 fail-closed · missing=${response.receipt.missing_required.length}`,
      )
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusyAction(null)
    }
  }, [enabled])

  const runRestore = useCallback(async (backup: RuntimeBackupSummary) => {
    if (!enabled) return
    const confirmed = window.confirm(
      `确认恢复 Runtime SQLite 备份？\n${backup.backup_id}\n这会替换当前 runtime 数据库，不包含项目正文/密钥。`,
    )
    if (!confirmed) return
    setBusyAction(backup.backup_id)
    setError(null)
    setNotice(null)
    try {
      const response = await restoreRuntimeBackup({
        backup_id: backup.backup_id,
        expected_manifest_hash: backup.manifest_hash,
      })
      setNotice(
        `已恢复 ${shortId(response.receipt.backup_id)} · pre=${response.receipt.pre_restore_backup_id ? shortId(response.receipt.pre_restore_backup_id) : 'none'}`,
      )
      await refresh()
    } catch (currentError) {
      setError(humanRuntimeError(currentError))
    } finally {
      setBusyAction(null)
    }
  }, [enabled, refresh])

  const busy = busyAction !== null
  const ready = readiness?.receipt.local_v1_ready === true
  const missingCount = readiness?.receipt.missing_required.length ?? 0
  const checkItems = useMemo(() => readiness?.receipt.checks ?? [], [readiness])

  return (
    <section className="runtime-ops-panel" aria-label="Runtime 运维" aria-busy={busy}>
      <header className="runtime-ops-header">
        <HardDriveDownload size={15} />
        <div>
          <strong>Runtime 运维</strong>
          <span>诊断 / 备份 / 发布就绪 · 不含密钥/正文/项目文件</span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新备份列表"
          title="刷新备份列表"
          disabled={!enabled || busy}
          onClick={() => void refresh()}
        >
          {busyAction === 'list' ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
        </button>
      </header>

      <div className="runtime-ops-actions">
        <button type="button" className="quiet-button" disabled={!enabled || busy} onClick={() => void runDiagnostics()}>
          {busyAction === 'diagnostics' ? <LoaderCircle size={13} className="spin" /> : <Download size={13} />}
          <span>导出诊断包</span>
        </button>
        <button type="button" className="quiet-button" disabled={!enabled || busy} onClick={() => void runCreate()}>
          {busyAction === 'create' ? <LoaderCircle size={13} className="spin" /> : <Archive size={13} />}
          <span>创建备份</span>
        </button>
        <button type="button" className="quiet-button" disabled={!enabled || busy} onClick={() => void runReadiness()}>
          {busyAction === 'readiness' ? <LoaderCircle size={13} className="spin" /> : ready ? <ShieldCheck size={13} /> : <ShieldAlert size={13} />}
          <span>评估发布就绪</span>
        </button>
      </div>

      {!enabled && (
        <div className="runtime-ops-empty">
          <AlertTriangle size={14} />
          <span>Runtime 未就绪，运维操作不可用</span>
        </div>
      )}
      {error && (
        <div className="agent-inline-error" role="alert">
          <AlertTriangle size={14} />
          <span>{error}</span>
        </div>
      )}
      {notice && (
        <div className="runtime-ops-notice" role="status">
          <span>{notice}</span>
        </div>
      )}

      {enabled && readiness && (
        <div className={`runtime-ops-readiness ${ready ? 'is-ready' : 'is-blocked'}`} aria-label="Local v1 发布就绪">
          <div className="runtime-ops-readiness-head">
            <span className={`runtime-ops-readiness-icon ${ready ? 'ok' : 'blocked'}`}>
              {ready ? <ShieldCheck size={14} /> : <ShieldAlert size={14} />}
            </span>
            <div>
              <strong>{ready ? 'Local v1 发布就绪' : 'Local v1 未就绪'}</strong>
              <span>
                local_v1_ready={String(readiness.receipt.local_v1_ready)}
                {' · '}
                local_v1_exe={String(readiness.receipt.local_v1_exe)}
                {' · '}
                formal_draft_allowed={String(readiness.receipt.formal_draft_allowed)}
                {' · '}
                missing={missingCount}
              </span>
            </div>
          </div>
          <ul className="runtime-ops-check-list">
            {checkItems.map((check) => (
              <li key={check.code} className={check.present ? 'present' : 'missing'}>
                <span className="runtime-ops-check-icon" aria-hidden="true">
                  {check.present ? <CheckCircle2 size={12} /> : <AlertTriangle size={12} />}
                </span>
                <div>
                  <strong title={check.code}>{CHECK_LABELS[check.code] ?? check.code}</strong>
                  <span>{check.detail}</span>
                </div>
              </li>
            ))}
          </ul>
          {readiness.not_claiming.length > 0 && (
            <p className="runtime-ops-not-claiming">
              不宣称：{readiness.not_claiming.slice(0, 5).join(' · ')}
              {readiness.not_claiming.length > 5 ? ` · +${readiness.not_claiming.length - 5}` : ''}
            </p>
          )}
        </div>
      )}

      {enabled && backups.length === 0 && !busy && (
        <div className="runtime-ops-empty">
          <Archive size={14} />
          <span>还没有 Runtime SQLite 备份</span>
        </div>
      )}

      {backups.length > 0 && (
        <ul className="runtime-ops-list">
          {backups.map((backup) => (
              <li key={backup.backup_id}>
                <div>
                  <strong title={backup.backup_id}>{shortId(backup.backup_id)}</strong>
                  <span>
                    schema={backup.database_schema_version} · {backup.integrity_check} · {(backup.database_size_bytes / 1024).toFixed(1)} KB
                  </span>
                </div>
                <button
                  type="button"
                  className="quiet-button"
                  disabled={!enabled || busy}
                  title="恢复该备份"
                  onClick={() => void runRestore(backup)}
                >
                  {busyAction === backup.backup_id ? <LoaderCircle size={13} className="spin" /> : <RotateCcw size={13} />}
                  <span>恢复</span>
                </button>
              </li>
          ))}
        </ul>
      )}
    </section>
  )
}

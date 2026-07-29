import {
  AlertTriangle,
  Check,
  CheckCheck,
  ChevronRight,
  FileDiff,
  FolderTree,
  GitBranch,
  Inbox,
  LoaderCircle,
  RefreshCw,
  RotateCcw,
  ShieldCheck,
  Undo2,
  X,
} from 'lucide-react'
import { useEffect, useState } from 'react'
import type {
  ChangeHunk,
  ChangeOperation,
  ChangeSet,
  ChangeSetStatus,
  HunkDecision,
  NlclOriginalNovelStateSyncReceipt,
  NlclOriginalPrewriteContextReportReceipt,
  NlclOriginalTruthContentExtractReceipt,
  NlclOriginalMandatoryUsagePlanReceipt,
  NlclOriginalMandatoryUsageClosureReceipt,
  NlclOriginalOperationPackageLikeReceipt,
  NlclOriginalLibraryLayoutInventoryReceipt,
  NlclOriginalLibraryLayoutCompletenessReceipt,
  NlclOriginalBoundedLibraryActivationReceipt,
  PostDraftGateCheck,
  PostDraftGateFileResult,
  PostDraftGateFinding,
  ReaderFindingDecisionValue,
  ReaderReviewDecisionReceipt,
  ReaderReviewReceipt,
  WritebackEvidenceReceipt,
} from '../api/changeSets'
import {
  createReaderReview,
  decideReaderReview,
  getFeedbackCandidates,
  getReaderReview,
  getReaderReviewDecision,
  getWritebackEvidence,
  promoteFeedbackCandidates,
  actFormalKnowledgePromotion,
  evaluateFormalKnowledgePromotion,
  exportEvalCorpus,
  getEvalCorpusExport,
  getFormalKnowledgePromotion,
  getOriginalNlclNovelStateSync,
  getOriginalNlclPrewriteContextReport,
  getOriginalNlclTruthContentExtract,
  getOriginalNlclMandatoryUsagePlan,
  getOriginalNlclMandatoryUsageClosure,
  getOriginalNlclOperationPackageLike,
  getOriginalNlclLibraryLayoutInventory,
  getOriginalNlclLibraryLayoutCompleteness,
  getOriginalNlclBoundedLibraryActivation,
  exportOriginalNlclPrewriteContextReport,
  exportOriginalNlclTruthContentExtract,
  exportOriginalNlclMandatoryUsagePlan,
  exportOriginalNlclMandatoryUsageClosure,
  exportOriginalNlclOperationPackageLike,
  exportOriginalNlclLibraryLayoutInventory,
  exportOriginalNlclLibraryLayoutCompleteness,
  exportOriginalNlclBoundedLibraryActivation,
  type FeedbackCandidate,
  type FeedbackCandidateBundle,
  type FormalKnowledgePromotionAction,
  type FormalKnowledgePromotionPackage,
  type FormalKnowledgePromotionReceipt,
} from '../api/changeSets'
import {
  deriveChangeSetGateState,
  hasAcceptedPostDraftBody,
  useChangeSets,
  type ChangeSetGateStatus,
  type ChangeSetReviewState,
} from '../hooks/useChangeSets'

interface ChangeSetReviewProps {
  projectId: string | null
  onProjectChanged: () => void
  preferredChangeSetId?: string | null
  preferredChangeSetRequestKey?: number
  review?: ChangeSetReviewState
}

const statusLabels: Record<ChangeSetStatus, string> = {
  reviewing: '待审',
  ready: '可应用',
  applying: '写入中',
  rolling_back: '回滚中',
  applied: '已应用',
  rolled_back: '已回滚',
  discarded: '已放弃',
  conflicted: '有冲突',
  failed: '失败',
}

const operationLabels: Record<ChangeOperation, string> = {
  create: '新增',
  update: '修改',
  delete: '删除',
}

const decisionLabels: Record<HunkDecision, string> = {
  pending: '未决定',
  accepted: '已接受',
  rejected: '已拒绝',
}

const gateStatusLabels: Record<ChangeSetGateStatus, string> = {
  not_required: '未启用',
  missing: '未运行',
  stale: '已过期',
  passed: '通过',
  blocked: '阻断',
  failed: '失败',
}

function gateFindingLocation(finding: PostDraftGateFinding): string {
  const { start, end } = finding.location
  const startPoint = `L${start.line}:C${start.column}`
  const endPoint = `L${end.line}:C${end.column}`
  return startPoint === endPoint ? startPoint : `${startPoint}-${endPoint}`
}

function gateFindings(files: PostDraftGateFileResult[], globalFindings: PostDraftGateFinding[]) {
  return [
    ...globalFindings.map((finding) => ({ path: null as string | null, finding })),
    ...files.flatMap((file) => file.findings.map((finding) => ({ path: file.path, finding }))),
  ]
}



function NlclStateChainPanel({ detail }: { detail: ChangeSet }) {
  const [syncReceipt, setSyncReceipt] = useState<NlclOriginalNovelStateSyncReceipt | null>(null)
  const [extractReceipt, setExtractReceipt] = useState<NlclOriginalTruthContentExtractReceipt | null>(null)
  const [planReceipt, setPlanReceipt] = useState<NlclOriginalMandatoryUsagePlanReceipt | null>(null)
  const [closureReceipt, setClosureReceipt] = useState<NlclOriginalMandatoryUsageClosureReceipt | null>(null)
  const [packageReceipt, setPackageReceipt] = useState<NlclOriginalOperationPackageLikeReceipt | null>(null)
  const [layoutReceipt, setLayoutReceipt] = useState<NlclOriginalLibraryLayoutInventoryReceipt | null>(null)
  const [completenessReceipt, setCompletenessReceipt] = useState<NlclOriginalLibraryLayoutCompletenessReceipt | null>(null)
  const [activationReceipt, setActivationReceipt] = useState<NlclOriginalBoundedLibraryActivationReceipt | null>(null)
  const [prewriteReceipt, setPrewriteReceipt] = useState<NlclOriginalPrewriteContextReportReceipt | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState<'extract' | 'plan' | 'closure' | 'package' | 'layout' | 'completeness' | 'activation' | 'prewrite' | null>(null)
  const [reloadKey, setReloadKey] = useState(0)
  const active = detail.status === 'applied' || detail.status === 'rolled_back'

  useEffect(() => {
    if (!active) {
      setSyncReceipt(null)
      setExtractReceipt(null)
      setPlanReceipt(null)
      setClosureReceipt(null)
      setPackageReceipt(null)
      setLayoutReceipt(null)
      setCompletenessReceipt(null)
      setActivationReceipt(null)
      setPrewriteReceipt(null)
      setError(null)
      return
    }
    let cancelled = false
    void Promise.all([
      getOriginalNlclNovelStateSync(detail.change_set_id),
      getOriginalNlclTruthContentExtract(detail.change_set_id),
      getOriginalNlclMandatoryUsagePlan(detail.change_set_id),
      getOriginalNlclMandatoryUsageClosure(detail.change_set_id),
      getOriginalNlclOperationPackageLike(detail.change_set_id),
      getOriginalNlclLibraryLayoutInventory(detail.change_set_id),
      getOriginalNlclLibraryLayoutCompleteness(detail.change_set_id),
      getOriginalNlclBoundedLibraryActivation(detail.change_set_id),
      getOriginalNlclPrewriteContextReport(detail.change_set_id),
    ])
      .then(([sync, extract, plan, closure, packageLike, layout, completeness, activation, prewrite]) => {
        if (cancelled) return
        setSyncReceipt(sync.receipt)
        setExtractReceipt(extract.receipt)
        setPlanReceipt(plan.receipt)
        setClosureReceipt(closure.receipt)
        setPackageReceipt(packageLike.receipt)
        setLayoutReceipt(layout.receipt)
        setCompletenessReceipt(completeness.receipt)
        setActivationReceipt(activation.receipt)
        setPrewriteReceipt(prewrite.receipt)
        setError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setSyncReceipt(null)
        setExtractReceipt(null)
        setPlanReceipt(null)
        setClosureReceipt(null)
        setPackageReceipt(null)
        setLayoutReceipt(null)
        setCompletenessReceipt(null)
        setActivationReceipt(null)
        setPrewriteReceipt(null)
        setError(err instanceof Error ? err.message : 'NLCL 状态链读取失败')
      })
    return () => {
      cancelled = true
    }
  }, [active, detail.change_set_id, detail.status, detail.updated_at, reloadKey])

  if (!active) return null

  const steps = [
    {
      key: 'sync',
      label: 'novel-state sync',
      done: Boolean(syncReceipt?.novel_state_synced),
      detail: syncReceipt
        ? `${syncReceipt.written_files.length} files · ${syncReceipt.receipt_id}`
        : '等待 state/*.json 同步',
    },
    {
      key: 'extract',
      label: 'truth content extract',
      done: Boolean(extractReceipt?.truth_content_extracted),
      detail: extractReceipt
        ? `角色 ${extractReceipt.extracted_character_count} · 伏笔 ${extractReceipt.extracted_foreshadow_count}`
        : '从 01-09 确定性抽取内容',
    },
    {
      key: 'plan',
      label: 'mandatory usage plan',
      done: Boolean(planReceipt?.mandatory_usage_plan_synced),
      detail: planReceipt
        ? `${planReceipt.items.length} items · plan scaffold`
        : '从抽取结果生成 usage plan scaffold',
    },
    {
      key: 'closure',
      label: 'mandatory usage closure',
      done: Boolean(closureReceipt?.mandatory_usage_closure_closed),
      detail: closureReceipt
        ? `${closureReceipt.items.length} decided · route_validated=false`
        : '作者裁决全部 plan 项后可 closed',
    },
    {
      key: 'package',
      label: 'operation_package_like',
      done: Boolean(packageReceipt?.operation_package_like_synced),
      detail: packageReceipt
        ? `${packageReceipt.readiness} · ${packageReceipt.operation_package_id}`
        : 'closure 后导出 risk_draft_only 执行包脚手架',
    },
    {
      key: 'layout',
      label: 'library layout inventory',
      done: Boolean(layoutReceipt?.library_layout_inventoried),
      detail: layoutReceipt
        ? `present ${layoutReceipt.present_slot_count} · missing ${layoutReceipt.missing_slot_count} · complete=false`
        : '盘点 01-09/state/chapter_context 槽位，不宣称全量布局完成',
    },
    {
      key: 'completeness',
      label: 'library layout completeness',
      done: Boolean(completenessReceipt?.library_layout_assessed),
      detail: completenessReceipt
        ? `${completenessReceipt.completeness_status} · present ${completenessReceipt.present_required_slot_count}/${completenessReceipt.required_slot_count} · whole_load_forbidden`
        : 'inventory 后评估 required slots 是否齐全，仍禁止整库加载',
    },
    {
      key: 'activation',
      label: 'bounded library activation',
      done: Boolean(activationReceipt?.bounded_library_activated),
      detail: activationReceipt
        ? `activated ${activationReceipt.activated_count}/${activationReceipt.max_sources} · chars ${activationReceipt.used_characters}/${activationReceipt.max_characters} · whole_load=false`
        : 'completeness 后有界激活 present slots，永不 whole-library load',
    },
    {
      key: 'prewrite',
      label: 'prewrite context report',
      done: Boolean(prewriteReceipt?.prewrite_context_report_synced),
      detail: prewriteReceipt
        ? `${prewriteReceipt.report_relative_path} · activated=${String(prewriteReceipt.bounded_library_activated)} · sources=${prewriteReceipt.activated_source_count}`
        : '导出 chapter_context 前情报告；若已 activation 会绑定有界源',
    },
  ] as const

  const chainStatus = activationReceipt
    ? {
        badge: 'gate-status-badge-passed' as const,
        label: `activated ${activationReceipt.activated_count}`,
        aria: `NLCL 状态链：bounded library activated ${activationReceipt.activated_count}/${activationReceipt.max_sources} · whole_load=false`,
      }
    : completenessReceipt
    ? {
        badge: completenessReceipt.library_layout_complete
          ? ('gate-status-badge-passed' as const)
          : ('gate-status-badge-stale' as const),
        label: completenessReceipt.library_layout_complete
          ? 'layout complete'
          : 'layout incomplete',
        aria: `NLCL 状态链：library layout ${completenessReceipt.completeness_status} · whole_load_forbidden`,
      }
    : layoutReceipt
    ? {
        badge: 'gate-status-badge-passed' as const,
        label: 'layout inventoried',
        aria: 'NLCL 状态链：library layout inventoried · complete=false',
      }
    : packageReceipt
      ? {
          badge: 'gate-status-badge-stale' as const,
          label: 'package ready',
          aria: 'NLCL 状态链：operation_package_like ready',
        }
      : closureReceipt
        ? {
            badge: 'gate-status-badge-passed' as const,
            label: 'closure',
            aria: 'NLCL 状态链：closure closed',
          }
        : syncReceipt
          ? {
              badge: 'gate-status-badge-stale' as const,
              label: '已同步',
              aria: 'NLCL 状态链：已同步',
            }
          : {
              badge: 'gate-status-badge-missing' as const,
              label: '未同步',
              aria: 'NLCL 状态链：未同步',
            }

  async function runExtract() {
    if (!syncReceipt) return
    setBusy('extract')
    try {
      const response = await exportOriginalNlclTruthContentExtract(
        detail.change_set_id,
        syncReceipt.receipt_hash,
      )
      setExtractReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '真相内容抽取失败')
    } finally {
      setBusy(null)
    }
  }

  async function runPlan() {
    if (!extractReceipt) return
    setBusy('plan')
    try {
      const response = await exportOriginalNlclMandatoryUsagePlan(
        detail.change_set_id,
        extractReceipt.receipt_hash,
      )
      setPlanReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'mandatory usage plan 导出失败')
    } finally {
      setBusy(null)
    }
  }

  async function runClosure() {
    if (!planReceipt) return
    setBusy('closure')
    try {
      const response = await exportOriginalNlclMandatoryUsageClosure(
        detail.change_set_id,
        planReceipt.receipt_hash,
        planReceipt.items.map((item) => ({
          item_id: item.item_id,
          status: 'used' as const,
          note: 'Workbench 作者一键裁决为 used',
        })),
        true,
      )
      setClosureReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'mandatory usage closure 关闭失败')
    } finally {
      setBusy(null)
    }
  }

  async function runPackage() {
    if (!closureReceipt) return
    setBusy('package')
    try {
      const response = await exportOriginalNlclOperationPackageLike(
        detail.change_set_id,
        closureReceipt.receipt_hash,
      )
      setPackageReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'operation_package_like 导出失败')
    } finally {
      setBusy(null)
    }
  }

  async function runLayout() {
    if (!packageReceipt) return
    setBusy('layout')
    try {
      const response = await exportOriginalNlclLibraryLayoutInventory(
        detail.change_set_id,
        packageReceipt.receipt_hash,
      )
      setLayoutReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'library layout inventory 导出失败')
    } finally {
      setBusy(null)
    }
  }

  async function runCompleteness() {
    if (!layoutReceipt) return
    setBusy('completeness')
    try {
      const response = await exportOriginalNlclLibraryLayoutCompleteness(
        detail.change_set_id,
        layoutReceipt.receipt_hash,
      )
      setCompletenessReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'library layout completeness 评估失败')
    } finally {
      setBusy(null)
    }
  }

  async function runActivation() {
    if (!completenessReceipt) return
    setBusy('activation')
    try {
      const response = await exportOriginalNlclBoundedLibraryActivation(
        detail.change_set_id,
        completenessReceipt.receipt_hash,
        16,
      )
      setActivationReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'bounded library activation 失败')
    } finally {
      setBusy(null)
    }
  }

  async function runPrewrite() {
    if (!syncReceipt) return
    setBusy('prewrite')
    try {
      const response = await exportOriginalNlclPrewriteContextReport(
        detail.change_set_id,
        syncReceipt.receipt_hash,
      )
      setPrewriteReceipt(response.receipt)
      setError(null)
      setReloadKey((value) => value + 1)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'prewrite 报告导出失败')
    } finally {
      setBusy(null)
    }
  }

  return (
    <section className="changeset-nlcl-chain" aria-label="NLCL 状态链">
      <header className="changeset-gate-header">
        <div className="changeset-gate-heading">
          <GitBranch size={15} />
          <div>
            <strong>NLCL 状态链</strong>
            <span>sync → extract → plan → closure → package → layout → completeness → activation → prewrite · 不解锁正式正文</span>
          </div>
        </div>
        <span className={`gate-status-badge ${chainStatus.badge}`} aria-label={chainStatus.aria}>
          {chainStatus.label}
        </span>
      </header>

      <ol className="nlcl-chain-timeline">
        {steps.map((step, index) => (
          <li key={step.key} className={step.done ? 'is-done' : 'is-pending'}>
            <span className="nlcl-chain-index" aria-hidden="true">{index + 1}</span>
            <div>
              <strong>{step.label}</strong>
              <span>{step.detail}</span>
            </div>
            <em>{step.done ? '完成' : '待执行'}</em>
          </li>
        ))}
      </ol>

      <div className="changeset-gate-note">
        <div>
          formal_draft_allowed=false · quality_assessed=false · route_validated=false ·
          mandatory_usage_closure_closed=
          {String(Boolean(closureReceipt?.mandatory_usage_closure_closed))}
        </div>
        <div>
          novel_state_synced={String(Boolean(syncReceipt?.novel_state_synced))} · extract=
          {String(Boolean(extractReceipt?.truth_content_extracted))} · plan=
          {String(Boolean(planReceipt?.mandatory_usage_plan_synced))} · prewrite=
          {String(Boolean(prewriteReceipt?.prewrite_context_report_synced))}
        </div>
        <div>
          package={String(Boolean(packageReceipt?.operation_package_like_synced))} · layout=
          {String(Boolean(layoutReceipt?.library_layout_inventoried))} · assessed=
          {String(Boolean(completenessReceipt?.library_layout_assessed))} · complete=
          {String(Boolean(completenessReceipt?.library_layout_complete))} · activated=
          {String(Boolean(activationReceipt?.bounded_library_activated))} · whole_load=
          {String(Boolean(activationReceipt?.whole_library_load ?? false))} · whole_load_forbidden=
          {String(
            Boolean(
              activationReceipt?.whole_library_load_forbidden ??
                completenessReceipt?.whole_library_load_forbidden ??
                layoutReceipt?.whole_library_load_forbidden ??
                true,
            ),
          )}
        </div>
      </div>

      {error && (
        <p className="changeset-gate-error" role="alert">
          {error}
        </p>
      )}

      <div className="nlcl-chain-actions">
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!syncReceipt || busy !== null}
          onClick={() => void runExtract()}
        >
          {busy === 'extract' ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
          {extractReceipt ? '重抽真相内容' : '抽取真相内容'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!extractReceipt || busy !== null}
          onClick={() => void runPlan()}
        >
          {busy === 'plan' ? <LoaderCircle size={13} className="spin" /> : <GitBranch size={13} />}
          {planReceipt ? '重导 usage plan' : '导出 usage plan'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!planReceipt || busy !== null}
          onClick={() => void runClosure()}
          title="将 plan 中全部项裁决为 used 并 finalize closed；仍不解锁正式正文"
        >
          {busy === 'closure' ? <LoaderCircle size={13} className="spin" /> : <CheckCheck size={13} />}
          {closureReceipt ? '重关 usage closure' : '关闭 usage closure'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!closureReceipt || busy !== null}
          onClick={() => void runPackage()}
        >
          {busy === 'package' ? <LoaderCircle size={13} className="spin" /> : <FileDiff size={13} />}
          {packageReceipt ? '重导 operation package' : '导出 operation package'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!packageReceipt || busy !== null}
          onClick={() => void runLayout()}
          title="盘点 01-09 / state / chapter_context 槽位；固定 complete=false，不整库加载"
        >
          {busy === 'layout' ? <LoaderCircle size={13} className="spin" /> : <FolderTree size={13} />}
          {layoutReceipt ? '重盘 library layout' : '盘点 library layout'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!layoutReceipt || busy !== null}
          onClick={() => void runCompleteness()}
          title="评估 required slots 是否齐全；complete 仍禁止 whole-library load，不解锁正式正文"
        >
          {busy === 'completeness' ? <LoaderCircle size={13} className="spin" /> : <ShieldCheck size={13} />}
          {completenessReceipt
            ? completenessReceipt.library_layout_complete
              ? '重评 layout complete'
              : '重评 layout incomplete'
            : '评估 layout completeness'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!completenessReceipt || busy !== null}
          onClick={() => void runActivation()}
          title="有界激活 present slots；固定 whole_library_load=false，不解锁正式正文"
        >
          {busy === 'activation' ? <LoaderCircle size={13} className="spin" /> : <Inbox size={13} />}
          {activationReceipt
            ? `重激活 library (${activationReceipt.activated_count})`
            : '有界激活 library'}
        </button>
        <button
          type="button"
          className="nlcl-chain-action"
          disabled={!syncReceipt || busy !== null}
          onClick={() => void runPrewrite()}
        >
          {busy === 'prewrite' ? <LoaderCircle size={13} className="spin" /> : <FileDiff size={13} />}
          {prewriteReceipt ? '重导 prewrite 报告' : '导出 prewrite 报告'}
        </button>
      </div>
    </section>
  )
}


function WritebackEvidencePanel({ detail }: { detail: ChangeSet }) {
  const [receipt, setReceipt] = useState<WritebackEvidenceReceipt | null>(null)
  const [historyCount, setHistoryCount] = useState(0)
  const [error, setError] = useState<string | null>(null)
  const active = detail.status === 'applied' || detail.status === 'rolled_back'

  useEffect(() => {
    if (!active) {
      setReceipt(null)
      setHistoryCount(0)
      setError(null)
      return
    }
    let cancelled = false
    void getWritebackEvidence(detail.change_set_id)
      .then((response) => {
        if (cancelled) return
        setReceipt(response.receipt)
        setHistoryCount(response.history.length)
        setError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setReceipt(null)
        setHistoryCount(0)
        setError(err instanceof Error ? err.message : '写回证据读取失败')
      })
    return () => {
      cancelled = true
    }
  }, [active, detail.change_set_id, detail.status, detail.updated_at])

  if (!active) return null

  return (
    <section className="changeset-writeback-evidence" aria-label="写回证据">
      <header className="changeset-gate-header">
        <div className="changeset-gate-heading">
          <FileDiff size={15} />
          <div>
            <strong>写回证据</strong>
            <span>Apply/Rollback 后的产品原生收据，不自动同步小说项目状态</span>
          </div>
        </div>
        <span className="gate-status-badge" aria-label={`写回证据状态：${receipt?.outcome ?? '读取中'}`}>
          {receipt?.outcome === 'applied' ? '已应用' : receipt?.outcome === 'rolled_back' ? '已回滚' : '读取中'}
        </span>
      </header>
      {error && <p className="changeset-gate-error" role="alert">{error}</p>}
      {receipt && (
        <div className="changeset-gate-note">
          <div>receipt · {receipt.receipt_id}</div>
          <div>result rev {receipt.result_revision} · history {historyCount}</div>
          <div>formal_draft_allowed=false · quality_assessed=false · novel_state_synced=false</div>
          {receipt.context_pack_id && <div>Context Pack · {receipt.context_pack_id}</div>}
          {receipt.route_receipt_id && <div>NLCL Route · {receipt.route_receipt_id}</div>}
        </div>
      )}
    </section>
  )
}


function ReaderReviewPanel({ detail }: { detail: ChangeSet }) {
  const [receipt, setReceipt] = useState<ReaderReviewReceipt | null>(null)
  const [decision, setDecision] = useState<ReaderReviewDecisionReceipt | null>(null)
  const [feedbackCount, setFeedbackCount] = useState(0)
  const [formalKnowledgeStatus, setFormalKnowledgeStatus] = useState<string | null>(null)
  const [formalKnowledgePackages, setFormalKnowledgePackages] = useState<FormalKnowledgePromotionPackage[]>([])
  const [evalCorpusPath, setEvalCorpusPath] = useState<string | null>(null)
  const [feedbackBundle, setFeedbackBundle] = useState<FeedbackCandidateBundle | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [localDecisions, setLocalDecisions] = useState<Record<string, ReaderFindingDecisionValue>>({})
  const active = detail.status === 'applied' || detail.status === 'rolled_back'
  const fingerprint = detail.writeback?.result_fingerprint ?? detail.rollback?.result_fingerprint ?? null

  useEffect(() => {
    if (!active) {
      setReceipt(null)
      setDecision(null)
      setFeedbackCount(0)
      setFeedbackBundle(null)
      setFormalKnowledgeStatus(null)
      setFormalKnowledgePackages([])
      setEvalCorpusPath(null)
      setError(null)
      setLocalDecisions({})
      return
    }
    let cancelled = false
    void Promise.all([
      getReaderReview(detail.change_set_id),
      getReaderReviewDecision(detail.change_set_id),
      getFeedbackCandidates(detail.change_set_id),
      getFormalKnowledgePromotion(detail.change_set_id),
      getEvalCorpusExport(detail.change_set_id),
    ])
      .then(([review, decisionResponse, feedback, formalKnowledge, evalCorpus]) => {
        if (cancelled) return
        setReceipt(review.receipt)
        setDecision(decisionResponse.receipt)
        setFeedbackCount(feedback.bundle?.candidates.length ?? 0)
        setFeedbackBundle(feedback.bundle)
        setFormalKnowledgeStatus(
          formalKnowledge.receipt
            ? `${formalKnowledge.receipt.status} · writes_formal_knowledge=false`
            : null,
        )
        setFormalKnowledgePackages([...(formalKnowledge.receipt?.packages ?? [])])
        setEvalCorpusPath(evalCorpus.receipt?.export_relative_path ?? null)
        const next: Record<string, ReaderFindingDecisionValue> = {}
        for (const item of decisionResponse.receipt?.decisions ?? []) {
          next[item.finding_id] = item.decision
        }
        setLocalDecisions(next)
        setError(null)
      })
      .catch((err: unknown) => {
        if (cancelled) return
        setError(err instanceof Error ? err.message : 'Reader 审阅读取失败')
      })
    return () => {
      cancelled = true
    }
  }, [active, detail.change_set_id, detail.status, detail.updated_at])

  if (!active) return null

  async function runReview() {
    if (!fingerprint) return
    setBusy(true)
    try {
      const response = await createReaderReview(detail.change_set_id, fingerprint)
      setReceipt(response.receipt)
      setError(null)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Reader 审阅失败')
    } finally {
      setBusy(false)
    }
  }

  async function finalizeDecisions() {
    if (!receipt) return
    const decisions = receipt.findings.map((finding) => ({
      finding_id: finding.finding_id,
      decision: localDecisions[finding.finding_id] ?? 'rejected',
    }))
    setBusy(true)
    try {
      const response = await decideReaderReview(
        detail.change_set_id,
        receipt.receipt_hash,
        decisions,
        true,
      )
      setDecision(response.receipt)
      const feedback = await getFeedbackCandidates(detail.change_set_id)
      setFeedbackCount(feedback.bundle?.candidates.length ?? 0)
      setFeedbackBundle(feedback.bundle)
      setError(null)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : 'Reader 决策失败')
    } finally {
      setBusy(false)
    }
  }

  async function promoteAdoptedToExperience() {
    if (!feedbackBundle) return
    const target = feedbackBundle.candidates.find(
      (item: FeedbackCandidate) =>
        item.source_decision === 'adopted' && item.promotion_status === 'not_promoted',
    )
    if (!target) {
      setError('没有可晋升的 adopted 候选')
      return
    }
    const steps = [
      'verify_source',
      'check_conflicts',
      'evaluate_regression',
      'approve',
      'record_experience',
    ] as const
    setBusy(true)
    try {
      let hash = feedbackBundle.receipt_hash
      let latestBundle = feedbackBundle
      for (const action of steps) {
        const response = await promoteFeedbackCandidates(detail.change_set_id, hash, [
          { candidate_id: target.candidate_id, action },
        ])
        if (!response.bundle) throw new Error('promotion response missing bundle')
        latestBundle = response.bundle
        hash = response.bundle.receipt_hash
      }
      setFeedbackBundle(latestBundle)
      setFeedbackCount(latestBundle.candidates.length)
      setError(null)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '反馈晋升失败')
    } finally {
      setBusy(false)
    }
  }


  function applyFormalKnowledgeReceipt(receipt: FormalKnowledgePromotionReceipt | null | undefined) {
    setFormalKnowledgeStatus(
      receipt
        ? `${receipt.status} · intent=${receipt.author_formalization_intent ?? 'none'} · effective=${String(receipt.author_formalization_effective)} · writes_formal_knowledge=${String(receipt.writes_formal_knowledge)}`
        : 'no receipt',
    )
    setFormalKnowledgePackages([...(receipt?.packages ?? [])])
  }

  async function actOnFormalKnowledge(action: FormalKnowledgePromotionAction) {
    if (!feedbackBundle) return
    let targetId: string | null = null
    if (action === 'stage_for_review') {
      targetId = feedbackBundle.candidates.find(
        (item: FeedbackCandidate) =>
          item.promotion_status === 'experience_recorded' && item.regression_result === 'pass_with_watch',
      )?.candidate_id ?? null
      if (!targetId) {
        setError('没有可 stage 的 experience_recorded 候选')
        return
      }
    } else if (action === 'materialize_for_review') {
      setBusy(true)
      try {
        const current = await getFormalKnowledgePromotion(detail.change_set_id)
        targetId = current.receipt?.items.find(
          (item) => item.promotion_status === 'formal_promotion_staged',
        )?.candidate_id ?? null
        if (!targetId) {
          // fall back to packages that are staged but not yet materialized
          targetId = current.receipt?.packages.find(
            (item) => !item.materialized_knowledge_candidate_id,
          )?.candidate_id ?? null
        }
        if (!targetId) {
          setError('没有可 materialize 的 formal_promotion_staged 候选')
          return
        }
        const response = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          { candidate_id: targetId, action: 'materialize_for_review' },
        ])
        applyFormalKnowledgeReceipt(response.receipt)
        setError(null)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '正式知识 materialize 失败')
      } finally {
        setBusy(false)
      }
      return
    } else if (
      action === 'author_request_formalization'
      || action === 'author_withhold_formalization'
    ) {
      setBusy(true)
      try {
        const current = await getFormalKnowledgePromotion(detail.change_set_id)
        targetId = current.receipt?.items.find(
          (item) =>
            item.promotion_status === 'formal_promotion_materialized'
            || item.promotion_status === 'formal_promotion_author_requested'
            || item.promotion_status === 'formal_promotion_author_withheld',
        )?.candidate_id ?? null
        if (!targetId) {
          targetId = current.receipt?.packages.find(
            (item) => Boolean(item.materialized_knowledge_candidate_id),
          )?.candidate_id ?? null
        }
        if (!targetId) {
          setError('没有可作者正式化决策的 materialized 候选')
          return
        }
        const response = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          { candidate_id: targetId, action },
        ])
        applyFormalKnowledgeReceipt(response.receipt)
        setError(null)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : `正式知识 ${action} 失败`)
      } finally {
        setBusy(false)
      }
      return
    } else if (action === 'commit_author_attested_formal_knowledge') {
      setBusy(true)
      try {
        const current = await getFormalKnowledgePromotion(detail.change_set_id)
        targetId = current.receipt?.items.find(
          (item) =>
            item.promotion_status === 'formal_promotion_author_requested'
            || item.promotion_status === 'formal_promotion_author_committed',
        )?.candidate_id ?? null
        if (!targetId) {
          setError('没有可提交的 author_requested 正式知识候选')
          return
        }
        const response = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          { candidate_id: targetId, action },
        ])
        applyFormalKnowledgeReceipt(response.receipt)
        setError(null)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '正式知识 author commit 失败')
      } finally {
        setBusy(false)
      }
      return
    } else if (action === 'rollback_author_attested_formal_knowledge') {
      setBusy(true)
      try {
        const current = await getFormalKnowledgePromotion(detail.change_set_id)
        targetId = current.receipt?.items.find(
          (item) => item.promotion_status === 'formal_promotion_author_committed',
        )?.candidate_id ?? null
        if (!targetId) {
          setError('没有可回滚的 author_committed 正式知识候选')
          return
        }
        const response = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          { candidate_id: targetId, action },
        ])
        applyFormalKnowledgeReceipt(response.receipt)
        setError(null)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '正式知识 author rollback 失败')
      } finally {
        setBusy(false)
      }
      return
    } else if (action === 'reject') {
      targetId = feedbackBundle.candidates.find(
        (item: FeedbackCandidate) => item.promotion_status === 'experience_recorded',
      )?.candidate_id ?? null
      if (!targetId) {
        setError('没有可 reject 的正式知识晋升候选')
        return
      }
    } else {
      // rollback_stage prefers currently staged package from formal-knowledge receipt path:
      // feedback bundle may still say experience_recorded after stage, so re-query receipt first.
      setBusy(true)
      try {
        const current = await getFormalKnowledgePromotion(detail.change_set_id)
        targetId = current.receipt?.items.find(
          (item) => item.promotion_status === 'formal_promotion_staged',
        )?.candidate_id ?? null
        if (!targetId) {
          setError('没有可 rollback 的 formal_promotion_staged 候选')
          return
        }
        const rolled = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          { candidate_id: targetId, action: 'rollback_stage' },
        ])
        applyFormalKnowledgeReceipt(rolled.receipt)
        setError(null)
      } catch (err: unknown) {
        setError(err instanceof Error ? err.message : '正式知识 rollback 失败')
      } finally {
        setBusy(false)
      }
      return
    }

    setBusy(true)
    try {
      if (action === 'stage_for_review') {
        await evaluateFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
          targetId,
        ])
      }
      const response = await actFormalKnowledgePromotion(detail.change_set_id, feedbackBundle.receipt_hash, [
        { candidate_id: targetId, action },
      ])
      applyFormalKnowledgeReceipt(response.receipt)
      setError(null)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : `正式知识 ${action} 失败`)
    } finally {
      setBusy(false)
    }
  }

  async function exportDesensitizedEvalCorpus() {
    setBusy(true)
    try {
      const response = await exportEvalCorpus(detail.change_set_id)
      setEvalCorpusPath(response.receipt?.export_relative_path ?? null)
      setError(null)
    } catch (err: unknown) {
      setError(err instanceof Error ? err.message : '评测语料导出失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="changeset-writeback-evidence" aria-label="冷读 Reader">
      <header className="changeset-gate-header">
        <div className="changeset-gate-heading">
          <ShieldCheck size={15} />
          <div>
            <strong>冷读 Reader</strong>
            <span>隔离意见，不覆盖 Gate，不授权正式正文</span>
          </div>
        </div>
        <button type="button" className="changeset-gate-run" disabled={busy || !fingerprint} onClick={() => void runReview()}>
          {busy ? <LoaderCircle size={14} className="spin" /> : <RefreshCw size={14} />}
          {receipt ? '重跑冷读' : '运行冷读'}
        </button>
      </header>
      {error && <p className="changeset-gate-error" role="alert">{error}</p>}
      {receipt && (
        <div className="changeset-gate-note">
          <div>mode · {receipt.review_mode}</div>
          <div>findings · {receipt.findings.length} · feedback candidates · {feedbackCount}</div>
          <div>formal_draft_allowed=false · quality_assessed=false · overrides_gate=false</div>
          <ul className="changeset-gate-findings">
            {receipt.findings.map((finding) => (
              <li key={finding.finding_id}>
                <strong>{finding.kind}/{finding.severity}</strong>
                <span>{finding.summary}</span>
                {!decision && (
                  <select
                    aria-label={`决策 ${finding.finding_id}`}
                    value={localDecisions[finding.finding_id] ?? 'rejected'}
                    disabled={busy}
                    onChange={(event) => {
                      const value = event.target.value as ReaderFindingDecisionValue
                      setLocalDecisions((current) => ({ ...current, [finding.finding_id]: value }))
                    }}
                  >
                    <option value="adopted">采纳</option>
                    <option value="rejected">拒绝</option>
                    <option value="deferred">暂缓</option>
                  </select>
                )}
              </li>
            ))}
          </ul>
          {!decision ? (
            <button type="button" className="changeset-apply" disabled={busy || receipt.findings.length === 0} onClick={() => void finalizeDecisions()}>
              确认决策并生成 follow-up
            </button>
          ) : (
            <div>
              <div>已决策 · parent invalid for new candidate · follow-up {decision.follow_up_change_set_id ?? 'none'}</div>
              <div>feedback candidates · {feedbackCount}</div>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void promoteAdoptedToExperience()}
              >
                一键推进经验库
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('stage_for_review')}
              >
                正式知识 stage-for-review
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('materialize_for_review')}
              >
                正式知识 materialize-for-review
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('author_request_formalization')}
              >
                作者请求正式化
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('author_withhold_formalization')}
              >
                作者暂缓正式化
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('commit_author_attested_formal_knowledge')}
              >
                作者确认写入正式知识
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('rollback_author_attested_formal_knowledge')}
              >
                回滚作者正式知识写入
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('reject')}
              >
                正式知识 reject
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy || !feedbackBundle}
                onClick={() => void actOnFormalKnowledge('rollback_stage')}
              >
                正式知识 rollback-stage
              </button>
              <button
                type="button"
                className="changeset-apply"
                disabled={busy}
                onClick={() => void exportDesensitizedEvalCorpus()}
              >
                导出脱敏评测语料
              </button>
              {formalKnowledgeStatus && <div>formal knowledge · {formalKnowledgeStatus}</div>}
              {formalKnowledgePackages.length > 0 && (
                <ul className="changeset-gate-findings" aria-label="正式知识 stage packages">
                  {formalKnowledgePackages.map((item) => (
                    <li key={item.package_id}>
                      <strong>{item.proposed_card_type}/{item.proposed_card_title}</strong>
                      <span>{item.summary}</span>
                      <span>
                        writes_formal_knowledge=false · package {item.package_id.slice(0, 24)}
                        {item.materialized_knowledge_candidate_id
                          ? ` · candidate ${item.materialized_knowledge_candidate_id.slice(0, 24)}`
                          : ''}
                      </span>
                    </li>
                  ))}
                </ul>
              )}
              {evalCorpusPath && <div>eval corpus · {evalCorpusPath} · training_ready=false</div>}
            </div>
          )}
        </div>
      )}
    </section>
  )
}

function GateReview({
  detail,
  gate,
  gateError,
  gateLoading,
  onRun,
  busy,
}: {
  detail: ChangeSet
  gate: ReturnType<typeof deriveChangeSetGateState>
  gateError: string | null
  gateLoading: boolean
  onRun: () => void
  busy: boolean
}) {
  const receipt = gate.receipt
  const result = receipt?.result
  const findings = gateFindings(result?.files ?? [], result?.global_findings ?? [])
  const hasAcceptedHunks = detail.changes.some((change) => (
    change.hunks.some((hunk) => hunk.decision === 'accepted')
  ))
  const hasAcceptedGatedHunks = hasAcceptedPostDraftBody(detail)
  const canRun = detail.status === 'ready' && gate.required
  const runLabel = gate.status === 'missing' ? '运行 Gate' : '重跑 Gate'
  const blocksApply = gate.required && gate.status !== 'passed'
  return (
    <section className={`changeset-gate gate-status-${gate.status}`} aria-label="Post-draft Gate" data-gate-status={gate.status}>
      <header className="changeset-gate-header">
        <div className="changeset-gate-heading">
          <ShieldCheck size={15} />
          <div>
            <strong>Post-draft Gate</strong>
            <span>{gate.required ? '接受正文写回前需要 Gate 收据' : detail.gate_required ? '当前接受项无需正文 Gate' : '当前 ChangeSet 不需要 Gate'}</span>
          </div>
        </div>
        <span className={`gate-status-badge gate-status-badge-${gate.status}`} aria-label={`Gate 状态：${gateStatusLabels[gate.status]}`}>{gateStatusLabels[gate.status]}</span>
      </header>
      {detail.gate_required && !hasAcceptedHunks && (
        <p className="changeset-gate-note">没有接受的 Hunk；放弃候选不会写回项目，无需 Gate 收据。</p>
      )}
      {detail.gate_required && hasAcceptedHunks && !hasAcceptedGatedHunks && (
        <p className="changeset-gate-note">当前接受的 Hunk 不属于正文 Gate 范围，无需 Gate 收据。</p>
      )}
      {gate.required && gate.status === 'stale' && (
        <p id={blocksApply ? 'changeset-gate-apply-note' : undefined} className="changeset-gate-note">审阅 revision 已变化，旧收据已隐藏。请重新运行 Gate。</p>
      )}
      {gate.required && gate.status === 'missing' && (
        <p id={blocksApply ? 'changeset-gate-apply-note' : undefined} className="changeset-gate-note">尚未针对当前候选运行 Gate，应用按钮保持禁用。</p>
      )}
      {gate.required && gate.status === 'failed' && (
        <p id={blocksApply ? 'changeset-gate-apply-note' : undefined} className="changeset-gate-note">Gate 执行失败，候选未获得写回授权。</p>
      )}
      {gate.required && gate.status === 'blocked' && (
        <p id={blocksApply ? 'changeset-gate-apply-note' : undefined} className="changeset-gate-note">Gate 发现阻断项，修订 Hunk 后需要重新运行。</p>
      )}
      {gate.required && gate.status === 'passed' && result && (
        <p className="changeset-gate-note">{result.summary} · 仅证明交付合同，不代表文学质量通过。</p>
      )}
      {gateError && <p className="changeset-gate-error" role="alert">{gateError}</p>}
      {receipt?.error_message && <p className="changeset-gate-error" role="alert">{receipt.error_code ? `${receipt.error_code}：` : ''}{receipt.error_message}</p>}
      {result && result.checks.length > 0 && (
        <div className="changeset-gate-checks" aria-label="Gate 检查项">
          {result.checks.map((check: PostDraftGateCheck) => (
            <div className={`changeset-gate-check check-${check.status}`} key={check.code}>
              <span className="gate-check-mark" aria-hidden="true">{check.status === 'passed' ? '✓' : '!'}</span>
              <div><strong>{check.code}</strong><span className="sr-only">检查状态：{check.status === 'passed' ? '通过' : '阻断'}</span><span>{check.message}</span>{check.evidence_refs.length > 0 && <code>{check.evidence_refs.join(' · ')}</code>}</div>
            </div>
          ))}
        </div>
      )}
      {findings.length > 0 && (
        <div className="changeset-gate-findings" aria-label="Gate 阻断定位">
          <div className="changeset-gate-subheading"><strong>定位</strong><span>{findings.length} 个阻断发现</span></div>
          {findings.map(({ path, finding }, index) => (
            <article className="changeset-gate-finding" key={`${path ?? 'global'}-${finding.code}-${index}`}>
              <div className="gate-finding-meta"><code>{path ?? '全局'}</code><span>{gateFindingLocation(finding)}</span></div>
              <strong>{finding.code}</strong>
              <p>{finding.message}</p>
              {finding.evidence && <pre>{finding.evidence}</pre>}
              {finding.failure_signature && <small>signature: {finding.failure_signature}</small>}
            </article>
          ))}
        </div>
      )}
      {canRun && (
        <button type="button" className="changeset-gate-run" disabled={busy || gateLoading} onClick={onRun}>
          {gateLoading ? <LoaderCircle size={13} className="spin" /> : <RefreshCw size={13} />}
          {gateLoading ? 'Gate 运行中' : runLabel}
        </button>
      )}
    </section>
  )
}

function hunkCounts(record: ChangeSet) {
  const decisions = record.changes.flatMap((change) =>
    change.hunks.map((hunk) => hunk.decision),
  )
  return {
    total: decisions.length,
    pending: decisions.filter((decision) => decision === 'pending').length,
    accepted: decisions.filter((decision) => decision === 'accepted').length,
    rejected: decisions.filter((decision) => decision === 'rejected').length,
  }
}

function compactTimestamp(value: string): string {
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return value
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

function lineRange(start: number, end: number): string {
  const firstLine = start + 1
  if (end <= start + 1) return `L${firstLine}`
  return `L${firstLine}-${end}`
}

interface HunkReviewProps {
  hunk: ChangeHunk
  path: string
  canDecide: boolean
  busy: boolean
  onDecide: (decision: 'accepted' | 'rejected') => void
}

function HunkReview({ hunk, path, canDecide, busy, onDecide }: HunkReviewProps) {
  const ordinal = hunk.index + 1
  return (
    <article className={`change-hunk hunk-${hunk.decision}`}>
      <header className="hunk-header">
        <div>
          <strong>Hunk {ordinal}</strong>
          <span>{lineRange(hunk.before_start, hunk.before_end)} → {lineRange(hunk.after_start, hunk.after_end)}</span>
        </div>
        <span className={`decision-badge decision-${hunk.decision}`}>{decisionLabels[hunk.decision]}</span>
      </header>
      <div className="hunk-diff" aria-label={`${path} Hunk ${ordinal} 差异`}>
        <section className="hunk-before">
          <span className="diff-side-label">修改前</span>
          <pre><code>{hunk.before_text || '（空）'}</code></pre>
        </section>
        <section className="hunk-after">
          <span className="diff-side-label">修改后</span>
          <pre><code>{hunk.after_text || '（空）'}</code></pre>
        </section>
      </div>
      <div className="hunk-actions">
        <button
          type="button"
          className={hunk.decision === 'accepted' ? 'decision-button is-accepted' : 'decision-button'}
          aria-label={`接受 ${path} 的 Hunk ${ordinal}`}
          title="接受这个 Hunk"
          disabled={!canDecide || busy || hunk.decision === 'accepted'}
          onClick={() => onDecide('accepted')}
        >
          <Check size={14} />接受
        </button>
        <button
          type="button"
          className={hunk.decision === 'rejected' ? 'decision-button is-rejected' : 'decision-button'}
          aria-label={`拒绝 ${path} 的 Hunk ${ordinal}`}
          title="拒绝这个 Hunk"
          disabled={!canDecide || busy || hunk.decision === 'rejected'}
          onClick={() => onDecide('rejected')}
        >
          <X size={14} />拒绝
        </button>
      </div>
    </article>
  )
}

function PanelState({
  kind,
  title,
  detail,
  onRetry,
}: {
  kind: 'loading' | 'empty' | 'error'
  title: string
  detail: string
  onRetry?: () => void
}) {
  const Icon = kind === 'loading' ? LoaderCircle : kind === 'error' ? AlertTriangle : Inbox
  return (
    <div className={`change-set-state change-set-state-${kind}`} role={kind === 'error' ? 'alert' : 'status'}>
      <Icon size={18} className={kind === 'loading' ? 'spin' : undefined} />
      <div><strong>{title}</strong><span>{detail}</span></div>
      {onRetry && <button type="button" className="quiet-button" onClick={onRetry}><RefreshCw size={14} />重试</button>}
    </div>
  )
}

interface ChangeSetReviewContentProps {
  projectId: string | null
  review: ChangeSetReviewState
}

function ManagedChangeSetReview({
  projectId,
  onProjectChanged,
  preferredChangeSetId = null,
  preferredChangeSetRequestKey = 0,
}: Omit<ChangeSetReviewProps, 'review'>) {
  const review = useChangeSets(
    projectId,
    onProjectChanged,
    preferredChangeSetId,
    preferredChangeSetRequestKey,
  )
  return <ChangeSetReviewContent projectId={projectId} review={review} />
}

function ChangeSetReviewContent({ projectId, review }: ChangeSetReviewContentProps) {
  const attentionCount = review.records.filter((record) =>
    ['reviewing', 'ready', 'conflicted', 'failed'].includes(record.status),
  ).length
  const counts = review.detail ? hunkCounts(review.detail) : null
  const canDecide = review.detail?.status === 'reviewing' || review.detail?.status === 'ready'
  const gate = review.gate
  const applyBlockedByGate = Boolean(
    gate.required
    && gate.status !== 'passed',
  )

  if (!projectId) {
    return (
      <div className="inspector-body changeset-panel changeset-panel-unbound">
        <PanelState kind="empty" title="尚未绑定项目" detail="绑定项目后，候选改动会在这里等待审阅" />
      </div>
    )
  }

  return (
    <div className="inspector-body changeset-panel">
      <div className="changeset-list-header">
        <div><strong>ChangeSets</strong><span>{attentionCount} 个需处理 · 共 {review.records.length} 个</span></div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新 ChangeSet"
          title="刷新 ChangeSet"
          disabled={review.listState === 'loading'}
          onClick={review.refresh}
        >
          <RefreshCw size={14} className={review.listState === 'loading' ? 'spin' : undefined} />
        </button>
      </div>

      {review.listState === 'loading' && review.records.length === 0 ? (
        <PanelState kind="loading" title="正在读取候选改动" detail="从 Writing Runtime 装载 ChangeSet 队列" />
      ) : review.listState === 'error' && review.records.length === 0 ? (
        <PanelState kind="error" title="ChangeSet 读取失败" detail={review.listError ?? '请重试'} onRetry={review.refresh} />
      ) : review.records.length === 0 ? (
        <PanelState kind="empty" title="没有候选改动" detail="Agent 或 Skill 产生的 ChangeSet 会出现在这里" />
      ) : (
        <div className="changeset-list" role="list" aria-label="ChangeSet 列表">
          {review.listError && (
            <div className="changeset-list-error" role="alert">
              <AlertTriangle size={13} />
              <span>{review.listError}</span>
              <button type="button" className="icon-button" aria-label="重试刷新 ChangeSet" title="重试刷新 ChangeSet" onClick={review.refresh}><RefreshCw size={12} /></button>
            </div>
          )}
          {review.records.map((record) => {
            return (
              <div role="listitem" key={record.change_set_id}>
                <button
                  type="button"
                  className={`changeset-row${review.selectedId === record.change_set_id ? ' is-active' : ''}`}
                  aria-current={review.selectedId === record.change_set_id ? 'true' : undefined}
                  onClick={() => review.select(record.change_set_id)}
                >
                  <span className={`changeset-status status-${record.status}`}>{statusLabels[record.status]}</span>
                  <span className="changeset-row-copy"><strong>{record.title}</strong><small>{record.file_count} 文件 · {record.pending_hunk_count} 待定 · {compactTimestamp(record.updated_at)}</small></span>
                  <ChevronRight size={14} />
                </button>
              </div>
            )
          })}
        </div>
      )}

      {review.records.length > 0 && (
        <div className="changeset-detail">
          {review.detailState === 'loading' ? (
            <PanelState kind="loading" title="正在读取 ChangeSet" detail="装载文件与 Hunk 差异" />
          ) : review.detailState === 'error' || !review.detail ? (
            <PanelState kind="error" title="ChangeSet 详情读取失败" detail={review.detailError ?? '详情不可用'} onRetry={review.refresh} />
          ) : (
            <>
              <header className="changeset-detail-header">
                <div className="changeset-title-line">
                  <span className={`changeset-status status-${review.detail.status}`}>{statusLabels[review.detail.status]}</span>
                  <span>base rev {review.detail.base_revision}</span>
                  <span>review rev {review.detail.review_revision}</span>
                </div>
                <h3>{review.detail.title}</h3>
                <p>{review.detail.summary}</p>
                {(review.detail.source_run_id || review.detail.source_tool_call_id) && (
                  <div className="changeset-provenance" aria-label="ChangeSet 来源">
                    <span>来源</span>
                    {review.detail.source_run_id && <code title={review.detail.source_run_id}>Run {review.detail.source_run_id}</code>}
                    {review.detail.source_tool_call_id && <code title={review.detail.source_tool_call_id}>Tool {review.detail.source_tool_call_id}</code>}
                  </div>
                )}
                <div className="changeset-counts" aria-label="Hunk 审阅统计">
                  <span><b>{counts?.accepted ?? 0}</b> 接受</span>
                  <span><b>{counts?.rejected ?? 0}</b> 拒绝</span>
                  <span><b>{counts?.pending ?? 0}</b> 待定</span>
                </div>
              </header>

              {review.detail.error && (
                <div className="changeset-inline-error" role="alert"><AlertTriangle size={15} /><span>{review.detail.error}</span></div>
              )}
              {review.actionError && review.actionError !== review.gateError && (
                <div className={`changeset-inline-error${review.actionConflict ? ' is-conflict' : ''}`} role="alert"><AlertTriangle size={15} /><span>{review.actionConflict ? `状态冲突：${review.actionError}` : review.actionError}</span><button type="button" className="icon-button" aria-label="同步最新 ChangeSet" title="同步最新 ChangeSet" onClick={review.refresh}><RefreshCw size={13} /></button></div>
              )}


              {canDecide && (
                <div className="changeset-batch-actions" aria-label="批量决策">
                  <button
                    type="button"
                    className="changeset-batch-action"
                    disabled={review.action !== null || (counts?.pending ?? 0) === 0}
                    onClick={() => {
                      const decisions = (review.detail?.changes ?? []).flatMap((change) =>
                        change.hunks
                          .filter((hunk) => hunk.decision === 'pending')
                          .map((hunk) => ({ hunk_id: hunk.hunk_id, decision: 'accepted' as const })),
                      )
                      void review.decideMany(decisions)
                    }}
                  >
                    <CheckCheck size={13} />
                    全部接受待定
                  </button>
                  <button
                    type="button"
                    className="changeset-batch-action is-reject"
                    disabled={review.action !== null || (counts?.pending ?? 0) === 0}
                    onClick={() => {
                      const decisions = (review.detail?.changes ?? []).flatMap((change) =>
                        change.hunks
                          .filter((hunk) => hunk.decision === 'pending')
                          .map((hunk) => ({ hunk_id: hunk.hunk_id, decision: 'rejected' as const })),
                      )
                      void review.decideMany(decisions)
                    }}
                  >
                    <X size={13} />
                    全部拒绝待定
                  </button>
                </div>
              )}

              <GateReview
                detail={review.detail}
                gate={gate}
                gateError={review.gateError}
                gateLoading={review.action?.kind === 'gate' || review.gateState === 'loading'}
                onRun={() => void review.runGate()}
                busy={review.action !== null}
              />

              <WritebackEvidencePanel detail={review.detail} />

              <NlclStateChainPanel detail={review.detail} />

              <ReaderReviewPanel detail={review.detail} />

              <div className="change-file-list">
                {review.detail.changes.map((change) => (
                  <section className="change-file" key={change.change_id}>
                    <header className="change-file-header">
                      <FileDiff size={15} />
                      <div><strong>{change.relative_path}</strong><span>{operationLabels[change.operation]} · {change.hunks.length} Hunk</span></div>
                      {canDecide && change.hunks.some((hunk) => hunk.decision === 'pending') && (
                        <div className="change-file-batch">
                          <button
                            type="button"
                            className="changeset-batch-action"
                            disabled={review.action !== null}
                            aria-label={`接受文件全部待定 Hunk：${change.relative_path}`}
                            onClick={() => {
                              const decisions = change.hunks
                                .filter((hunk) => hunk.decision === 'pending')
                                .map((hunk) => ({ hunk_id: hunk.hunk_id, decision: 'accepted' as const }))
                              void review.decideMany(decisions)
                            }}
                          >
                            <Check size={12} />
                            本文件接受
                          </button>
                          <button
                            type="button"
                            className="changeset-batch-action is-reject"
                            disabled={review.action !== null}
                            aria-label={`拒绝文件全部待定 Hunk：${change.relative_path}`}
                            onClick={() => {
                              const decisions = change.hunks
                                .filter((hunk) => hunk.decision === 'pending')
                                .map((hunk) => ({ hunk_id: hunk.hunk_id, decision: 'rejected' as const }))
                              void review.decideMany(decisions)
                            }}
                          >
                            <X size={12} />
                            本文件拒绝
                          </button>
                        </div>
                      )}
                    </header>
                    <p className="change-rationale">{change.rationale}</p>
                    {change.hunks.map((hunk) => (
                      <HunkReview
                        key={hunk.hunk_id}
                        hunk={hunk}
                        path={change.relative_path}
                        canDecide={canDecide}
                        busy={review.action !== null}
                        onDecide={(decision) => void review.decide({ hunk_id: hunk.hunk_id, decision })}
                      />
                    ))}
                  </section>
                ))}
              </div>

              <footer className="changeset-actions" aria-live="polite" aria-atomic="true">
                {review.detail.status === 'ready' ? (
                  <button type="button" className="changeset-apply" disabled={review.action !== null || applyBlockedByGate} aria-describedby={applyBlockedByGate ? 'changeset-gate-apply-note' : undefined} title={applyBlockedByGate ? '请先运行并通过当前 revision 的 Gate' : undefined} onClick={() => void review.apply()}>
                    {review.action?.kind === 'apply' ? <LoaderCircle size={15} className="spin" /> : (counts?.accepted ?? 0) > 0 ? <CheckCheck size={15} /> : <X size={15} />}
                    {(counts?.accepted ?? 0) > 0 ? '应用所选更改' : '放弃这组改动'}
                  </button>
                ) : review.detail.status === 'applied' ? (
                  <button type="button" className="changeset-rollback" disabled={review.action !== null} onClick={() => void review.rollback()}>
                    {review.action?.kind === 'rollback' ? <LoaderCircle size={15} className="spin" /> : <Undo2 size={15} />}
                    回滚 ChangeSet
                  </button>
                ) : review.detail.status === 'reviewing' ? (
                  <span className="review-progress"><RotateCcw size={14} />还有 {counts?.pending ?? 0} 个 Hunk 未决定</span>
                ) : review.detail.status === 'applying' ? (
                  <span className="review-progress is-running"><LoaderCircle size={14} className="spin" />Writing Runtime 正在写入项目</span>
                ) : review.detail.status === 'rolling_back' ? (
                  <span className="review-progress is-running"><LoaderCircle size={14} className="spin" />Writing Runtime 正在恢复项目</span>
                ) : review.detail.status === 'conflicted' ? (
                  <span className="review-progress is-conflict"><AlertTriangle size={14} />项目内容已变化，请基于最新项目重新生成候选</span>
                ) : review.detail.status === 'failed' ? (
                  <span className="review-progress is-failed"><AlertTriangle size={14} />本次写回失败，请检查项目状态后重新生成候选</span>
                ) : review.detail.status === 'discarded' ? (
                  <span className="review-progress"><X size={14} />这组改动已放弃，未写入项目</span>
                ) : review.detail.status === 'rolled_back' && review.detail.rollback ? (
                  <span className="review-progress"><Check size={14} />项目结果 revision {review.detail.rollback.result_revision}</span>
                ) : review.detail.writeback ? (
                  <span className="review-progress"><Check size={14} />项目结果 revision {review.detail.writeback.result_revision}</span>
                ) : (
                  <span className="review-progress">当前状态不允许继续操作</span>
                )}
              </footer>
            </>
          )}
        </div>
      )}
    </div>
  )
}

export function ChangeSetReview({ review, ...props }: ChangeSetReviewProps) {
  return review
    ? <ChangeSetReviewContent projectId={props.projectId} review={review} />
    : <ManagedChangeSetReview {...props} />
}

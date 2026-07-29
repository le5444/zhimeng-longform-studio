import {
  AlertTriangle,
  CheckCircle2,
  CircleDot,
  Copy,
  FileSearch,
  LoaderCircle,
  Play,
  RefreshCw,
  Route,
  Trash2,
} from 'lucide-react'
import {
  projectKbContractBindingStatus,
  type ContextPackRecord,
  type KnowledgeProvenanceRef,
  type NlclRouteExecutionState,
  type NlclRouteReceipt,
  type PrewriteOperationPackage,
  type ProjectKbContractBindingStatus,
} from '../api/contextPacks'
import type { AgentConversationState } from '../hooks/useAgentConversation'
import type { WriterMode } from '../api/agent'

interface ContextPackReviewProps {
  record: ContextPackRecord
  nlclRoute: NlclRouteReceipt | null
  routeExecutions?: NlclRouteExecutionState[]
  operationPackage?: PrewriteOperationPackage | null
  state: AgentConversationState['contextState']
  staleReasons: string[]
  agentAvailable: boolean
  agentDetail: string
  runActive: boolean
  error: string | null
  readOnly?: boolean
  writerMode?: WriterMode
  onWriterModeChange?: (mode: WriterMode) => void
  onConfirm?: () => Promise<void>
  onDiscard?: () => void
  onRefresh?: () => Promise<void>
}

const stateLabels: Record<AgentConversationState['contextState'], string> = {
  idle: '待审查',
  restoring: '恢复中',
  compiling: '重新编译中',
  ready: '可运行',
  degraded: '有警告',
  blocked: '已阻断',
  stale: '已过期',
  confirming: '正在启动 Run',
}

const roleLabels = {
  system: 'System',
  user: 'User',
  assistant: 'Assistant',
} as const

const executionLabels: Record<NlclRouteExecutionState['status'], string> = {
  awaiting_execution: '等待 Runtime 执行',
  running: 'Runtime 执行中',
  succeeded: '执行成功',
  failed: '执行失败',
  cancelled: '执行已取消',
}

const projectKbBindingLabels: Record<ProjectKbContractBindingStatus, {
  title: string
  detail: string
}> = {
  checking: {
    title: '正在核对身份',
    detail: '等待 NLCL 路由收据后核对链内绑定。',
  },
  current: {
    title: '身份已绑定',
    detail: 'Pack、Route 与已有执行收据的 .kb 身份链内一致；运行前仍会复核磁盘状态。',
  },
  legacy: {
    title: '旧记录 · 仅可查看',
    detail: '历史记录没有 .kb 身份，必须重新编译后才能运行。',
  },
  mismatch: {
    title: '绑定不匹配',
    detail: 'Pack、Route 或执行收据指向了不同的 .kb 身份，禁止运行。',
  },
}

function copyValue(value: string | null | undefined) {
  if (!value) return
  void navigator.clipboard?.writeText(value).catch(() => undefined)
}

function TechValue({
  label,
  value,
}: {
  label: string
  value: string | null | undefined
}) {
  return (
    <span className="context-tech-value">
      <code>{value ?? '未记录'}</code>
      {value && <button type="button" className="context-copy-button" aria-label={`复制${label}`} onClick={() => copyValue(value)}><Copy size={11} />复制</button>}
    </span>
  )
}

function routeSourceLabel(source: string): string {
  if (source === 'native_knowledge') return '本项目正式知识'
  if (source === 'external_distillation') return '外部蒸馏知识'
  if (source === 'unavailable') return '知识不可用'
  return source
}

function routeStatusLabel(status: string): string {
  if (status === 'ready') return '可用于生成待审候选'
  if (status === 'degraded') return '可用但有提醒'
  if (status === 'unavailable') return '不可用'
  return status
}

function workflowStepLabel(skillId: string): string {
  if (skillId.endsWith('.project-audit')) return '项目审计'
  if (skillId.endsWith('.prewrite-gate')) return '写前门禁'
  if (skillId.endsWith('.creation-handoff')) return '创作交接'
  return skillId
}

function writePreChainLabel(skillIds: string[]): string {
  if (skillIds.length === 0) return '项目审计 → 写前门禁 → 创作交接'
  return skillIds.map((skillId) => workflowStepLabel(skillId)).join(' → ')
}

function executionAuthorSummary(
  execution: NlclRouteExecutionState,
  skillId?: string,
): string {
  const step = skillId ? workflowStepLabel(skillId) : '写前检查'
  if (execution.status === 'awaiting_execution') return `等待${step}`
  if (execution.status === 'running') return `正在执行${step}`
  if (execution.status === 'succeeded') return `${step}已完成，正文仍未写回`
  if (execution.status === 'failed') return `${step}失败，需要先处理`
  return `${step}已取消`
}

function domainStatusLabel(status: string | null | undefined): string {
  if (status === 'blocked') return '仅允许生成待审候选'
  if (status === 'risk_draft_only') return '仅允许风险草案'
  if (status === 'passed') return '已通过'
  if (!status) return '等待结果'
  return status
}

function formatNumber(value: number): string {
  return value.toLocaleString('zh-CN')
}

function budgetPercent(used: number, maximum: number): number {
  return Math.min(100, Math.round((used / maximum) * 100))
}

function KnowledgeProvenanceTrace({
  title,
  provenance,
}: {
  title: string
  provenance: KnowledgeProvenanceRef | null | undefined
}) {
  if (!provenance) return null

  return (
    <div className="context-fragment-provenance" aria-label={`${title} 知识来源链`}>
      <code>source_import_id · {provenance.source_import_id}</code>
      <code>candidate_id · {provenance.candidate_id}</code>
      <code>version_id · {provenance.version_id}</code>
      <code>version_lineage_receipt_id · {provenance.version_lineage_receipt_id}</code>
      <code>formalization_receipt_id · {provenance.formalization_receipt_id}</code>
      <code>formalization_receipt_hash · {provenance.formalization_receipt_hash}</code>
    </div>
  )
}

export function ContextPackReview({
  record,
  nlclRoute,
  routeExecutions = [],
  operationPackage = null,
  state,
  staleReasons,
  agentAvailable,
  agentDetail,
  runActive,
  error,
  readOnly = false,
  writerMode = 'repair_only',
  onWriterModeChange,
  onConfirm,
  onDiscard,
  onRefresh,
}: ContextPackReviewProps) {
  const { pack } = record
  const blockingGates = pack.gates.filter((gate) => gate.severity === 'blocking').length
  const warningGates = pack.gates.length - blockingGates
  const operating = state === 'compiling' || state === 'confirming' || state === 'restoring'
  const prewriteRequiresRecompile = operationPackage?.missing_requirements.includes(
    'migrated_prewrite_package_requires_recompile',
  ) ?? false
  const projectKbBindingStatus = projectKbContractBindingStatus(record, nlclRoute, routeExecutions)
  const projectKbBinding = projectKbBindingLabels[projectKbBindingStatus]
  const writePreSkillIds = (nlclRoute?.chapter_call_list ?? []).map((call) => call.skill_id)
  const writePreChain = writePreChainLabel(writePreSkillIds)
  const skillIdByOrdinal = new Map(
    (nlclRoute?.chapter_call_list ?? []).map((call) => [call.ordinal, call.skill_id]),
  )
  const readinessChecks: Array<{ id: string; label: string; ok: boolean; detail: string }> = [
    {
      id: 'agent',
      label: 'Writing Agent',
      ok: agentAvailable,
      detail: agentAvailable ? '可启动受管写前链' : agentDetail,
    },
    {
      id: 'pack',
      label: 'Context Pack',
      ok: pack.readiness !== 'blocked' && state !== 'stale' && staleReasons.length === 0,
      detail: pack.readiness === 'blocked'
        ? '存在阻断 Gate，不能确认运行'
        : state === 'stale' || staleReasons.length > 0
          ? (staleReasons[0] ?? '当前 Pack 已过期')
          : '绑定仍对应当前项目/会话/章节',
    },
    {
      id: 'kb',
      label: '项目知识绑定',
      ok: projectKbBindingStatus === 'current',
      detail: projectKbBinding.detail,
    },
    {
      id: 'route',
      label: 'NLCL 路由',
      ok: Boolean(nlclRoute),
      detail: nlclRoute
        ? `${nlclRoute.chapter_call_list.length} 步写前链已绑定`
        : 'NLCL 路由收据尚未就绪',
    },
    {
      id: 'prewrite',
      label: '写前运行包',
      ok: Boolean(operationPackage) && !prewriteRequiresRecompile,
      detail: !operationPackage
        ? '写前运行包尚未就绪'
        : prewriteRequiresRecompile
          ? '写前运行包来自旧版本，必须重新编译'
          : '运行包已与当前 Pack / Route 绑定',
    },
    {
      id: 'run',
      label: '当前 Run',
      ok: !runActive && !operating,
      detail: runActive
        ? '当前 Run 尚未结束'
        : operating
          ? stateLabels[state]
          : '没有进行中的 Run',
    },
  ]
  const confirmBlockers = readinessChecks
    .filter((check) => !check.ok)
    .map((check) => `${check.label}：${check.detail}`)
  const writePreReady = confirmBlockers.length === 0
  const confirmDisabled = !writePreReady
  const executionByOrdinal = new Map(
    routeExecutions.map((execution) => [execution.call_ordinal, execution]),
  )

  return (
    <div
      className="inspector-body context-pack-panel"
      role="tabpanel"
      id="inspector-panel-context"
      aria-labelledby="inspector-tab-context"
    >
      <header className={`context-pack-status context-pack-${state}`}>
        <span aria-hidden="true">
          {operating
            ? <LoaderCircle size={17} className="spin" />
            : state === 'stale' || pack.readiness === 'blocked' || prewriteRequiresRecompile
              ? <AlertTriangle size={17} />
              : pack.readiness === 'ready'
                ? <CheckCircle2 size={17} />
                : <CircleDot size={17} />}
        </span>
        <div>
          <strong>Context Pack · {readOnly ? '已绑定 Run' : stateLabels[state]}</strong>
          <span>{pack.profile} · project rev {pack.project_revision} · history {pack.history_through_ordinal}</span>
        </div>
      </header>

      {staleReasons.length > 0 && (
        <section className="context-stale-notice" role="alert" aria-label="Context Pack 已过期">
          <AlertTriangle size={15} />
          <div><strong>当前 Pack 不能运行</strong><ul>{staleReasons.map((reason) => <li key={reason}>{reason}</li>)}</ul></div>
        </section>
      )}

      <section className="context-pack-objective" aria-labelledby="context-objective-title">
        <span id="context-objective-title">本次任务</span>
        <p>{pack.objective}</p>
        <dl>
          <div><dt>章节</dt><dd>{pack.active_chapter_id ?? '未指定'}</dd></div>
          <div><dt>正式知识</dt><dd>{pack.knowledge_fingerprint ? '已装配并等待作者确认运行' : '未装配'}</dd></div>
        </dl>
      </section>

      <section className="context-author-summary" aria-label="作者运行结论">
        <article>
          <CheckCircle2 size={15} />
          <div><strong>知识已绑定</strong><span>{nlclRoute ? `${nlclRoute.selected_cards.length} 张正式知识卡进入本次写作上下文` : '正在读取知识路由'}</span></div>
        </article>
        <article>
          <FileSearch size={15} />
          <div><strong>{writerMode === 'author_review_change_proposal' ? '只生成待审正文候选' : '只生成修复建议'}</strong><span>Writing Agent 不会直接改项目文件，候选要经过作者审阅。</span></div>
        </article>
        <article>
          <AlertTriangle size={15} />
          <div><strong>正文尚未写回</strong><span>Gate、Apply 和回滚仍在候选审阅之后，不在此步自动发生。</span></div>
        </article>
      </section>

      {!readOnly && (
        <section
          className={`context-write-pre-readiness ${writePreReady ? 'write-pre-ready' : 'write-pre-blocked'}`}
          aria-label="写前链就绪"
        >
          {writePreReady ? <CheckCircle2 size={15} /> : <AlertTriangle size={15} />}
          <div>
            <strong>{writePreReady ? '写前链就绪 · 可确认运行' : '写前链未就绪 · 确认已锁定'}</strong>
            <span>确认后进入：{writePreChain}</span>
            <span>formal_draft_allowed=false · 本步不写正式正文</span>
            <ul className="context-write-pre-checks">
              {readinessChecks.map((check) => (
                <li key={check.id} className={check.ok ? 'check-ok' : 'check-blocked'}>
                  <strong>{check.ok ? '通过' : '阻断'} · {check.label}</strong>
                  <span>{check.detail}</span>
                </li>
              ))}
            </ul>
            {!writePreReady && confirmBlockers.length > 0 && (
              <ul className="context-write-pre-blockers" aria-label="确认阻断原因">
                {confirmBlockers.map((reason) => <li key={reason}>{reason}</li>)}
              </ul>
            )}
          </div>
        </section>
      )}

      <section className={`context-kb-binding kb-binding-${projectKbBindingStatus}`} aria-label="项目知识绑定">
        {projectKbBindingStatus === 'current'
          ? <CheckCircle2 size={15} />
          : projectKbBindingStatus === 'checking'
            ? <LoaderCircle size={15} className="spin" />
            : <AlertTriangle size={15} />}
        <div>
          <strong>{projectKbBinding.title}</strong>
          <span>{projectKbBinding.detail}</span>
          <ul className="context-human-list">
            <li>使用当前项目知识快照，不读取整库。</li>
            <li>运行前会再次校验磁盘与 Runtime 记录。</li>
          </ul>
          <details className="context-tech-details">
            <summary>技术证据</summary>
            <dl>
              <div><dt>state_id</dt><dd><TechValue label="state id" value={pack.project_kb_state_id} /></dd></div>
              <div><dt>manifest_hash</dt><dd><TechValue label="manifest hash" value={pack.project_kb_manifest_hash} /></dd></div>
              <div><dt>knowledge_fingerprint</dt><dd><TechValue label="knowledge fingerprint" value={pack.knowledge_fingerprint} /></dd></div>
            </dl>
          </details>
        </div>
      </section>

      <section className="nlcl-route-section" aria-labelledby="nlcl-route-title">
        <div className="context-section-heading">
          <h3 id="nlcl-route-title"><Route size={15} />写作上下文路线</h3>
          <span>{nlclRoute ? routeStatusLabel(nlclRoute.status) : '读取中'}</span>
        </div>
        {nlclRoute ? <>
          <div className={`nlcl-route-summary route-${nlclRoute.status}`}>
            <div><span>知识来源</span><strong>{routeSourceLabel(nlclRoute.source)}</strong></div>
            <div><span>选中知识</span><strong>{nlclRoute.selected_cards.length} 张</strong></div>
            <div><span>写回权限</span><strong>{nlclRoute.formal_draft_allowed ? '已授权' : '未写回'}</strong></div>
          </div>
          <div className="nlcl-boundary" role="note">
            <AlertTriangle size={14} />
            <span>当前只允许生成待审候选；正式正文不会在此步骤自动写回。</span>
          </div>
          <div className="nlcl-execution-list">
            {routeExecutions.map((execution) => (
              <div className={`nlcl-execution-state execution-${execution.status}`} role="status" key={execution.call_ordinal}>
                {execution.status === 'running'
                  ? <LoaderCircle size={15} className="spin" />
                  : execution.status === 'succeeded'
                    ? <CheckCircle2 size={15} />
                    : execution.status === 'awaiting_execution'
                      ? <CircleDot size={15} />
                      : <AlertTriangle size={15} />}
                <div>
                  <strong>{execution.call_ordinal}. {executionAuthorSummary(execution, skillIdByOrdinal.get(execution.call_ordinal) ?? execution.receipt?.skill_id)}</strong>
                  <span>
                    {execution.receipt?.domain_status
                      ? domainStatusLabel(execution.receipt.domain_status)
                      : execution.error_message ?? '等待对应受管 Creation 调用'}
                  </span>
                  {execution.tool_call_id && <details className="context-inline-tech"><summary>技术状态</summary><TechValue label={`execution ${execution.call_ordinal} tool call`} value={execution.tool_call_id} /></details>}
                </div>
              </div>
            ))}
          </div>
          <div className="nlcl-card-list">
            {nlclRoute.selected_cards.length === 0 && <p className="context-empty-row">没有知识卡进入本次路由</p>}
            {nlclRoute.selected_cards.map((card) => (
              <article key={card.card_id}>
                <div><strong>{card.title}</strong><span>{card.card_type} · {card.version_id}</span></div>
                <code>{card.card_id}</code>
                {card.route_terms.length > 0 && <small>{card.route_terms.join(' · ')}</small>}
              </article>
            ))}
          </div>
          <ol className="nlcl-call-list">
            {nlclRoute.chapter_call_list.map((call) => (
              <li key={`${call.ordinal}:${call.skill_id}`}>
                <strong>{call.ordinal}. {workflowStepLabel(call.skill_id)}</strong>
                <span>{call.purpose}</span>
                <code>{call.skill_id}</code>
                <code>{executionLabels[executionByOrdinal.get(call.ordinal)?.status ?? 'awaiting_execution'] ?? call.execution_status}</code>
              </li>
            ))}
          </ol>
          <details className="context-disclosure context-route-tech">
            <summary><span>路由技术详情</span><b>{nlclRoute.route_receipt_id}</b></summary>
            <dl className="context-tech-grid">
              <div><dt>source</dt><dd><TechValue label="route source" value={nlclRoute.source} /></dd></div>
              <div><dt>status</dt><dd><TechValue label="route status" value={nlclRoute.status} /></dd></div>
              <div><dt>route_receipt_id</dt><dd><TechValue label="route receipt id" value={nlclRoute.route_receipt_id} /></dd></div>
              <div><dt>route_receipt_hash</dt><dd><TechValue label="route receipt hash" value={nlclRoute.receipt_hash} /></dd></div>
              <div><dt>context_pack_hash</dt><dd><TechValue label="context pack hash" value={nlclRoute.context_pack_hash} /></dd></div>
            </dl>
          </details>
          {(nlclRoute.strict_failures.length > 0 || nlclRoute.warnings.length > 0) && (
            <details className="context-disclosure nlcl-route-codes">
              <summary><span>路由边界</span><b>{nlclRoute.strict_failures.length} 失败 · {nlclRoute.warnings.length} 提醒</b></summary>
              <ul>
                {nlclRoute.strict_failures.map((code) => <li key={`failure:${code}`}><strong>失败</strong><code>{code}</code></li>)}
                {nlclRoute.warnings.map((code) => <li key={`warning:${code}`}><span>提醒</span><code>{code}</code></li>)}
              </ul>
            </details>
          )}
        </> : <p className="context-empty-row">正在读取与 Context Pack 原子绑定的路由收据</p>}
      </section>

      {operationPackage && (
        <details className="context-disclosure prewrite-operation-package">
          <summary>
            <span>写作前检查包</span>
            <b>{domainStatusLabel(operationPackage.readiness)} · {operationPackage.missing_requirements.length} 项待补</b>
          </summary>
          <div className="nlcl-route-summary">
            <div><span>场景信息</span><strong>{operationPackage.scene_contract.status === 'bound' ? '已绑定' : '未绑定'}</strong></div>
            <div><span>风格知识</span><strong>{operationPackage.style_output_contract.status === 'bound' ? '已绑定' : '未绑定'}</strong></div>
            <div><span>正文写回</span><strong>{operationPackage.formal_draft_allowed ? '已授权' : '未写回'}</strong></div>
          </div>
          {operationPackage.missing_requirements.length > 0 && (
            <ul className="nlcl-route-codes">
              {operationPackage.missing_requirements.map((code) => <li key={code}><strong>缺失</strong><code>{code}</code></li>)}
            </ul>
          )}
          <div className="context-gate-list">
            {operationPackage.failure_routing.map((route) => (
              <div className="context-gate-row gate-warning" key={route.failure_signature}>
                <CircleDot size={14} />
                <div><strong>{route.failure_signature}</strong><span>失败后返回 {route.rewrite_from}</span></div>
              </div>
            ))}
          </div>
          <details className="context-tech-details">
            <summary>技术证据</summary>
            <dl>
              <div><dt>operation_package_id</dt><dd><TechValue label="operation package id" value={operationPackage.operation_package_id} /></dd></div>
              <div><dt>package_hash</dt><dd><TechValue label="prewrite package hash" value={operationPackage.package_hash} /></dd></div>
              <div><dt>route_receipt_hash</dt><dd><TechValue label="prewrite route hash" value={operationPackage.route_receipt_hash} /></dd></div>
            </dl>
          </details>
        </details>
      )}

      <section className="context-budget" aria-labelledby="context-budget-title">
        <div className="context-section-heading"><h3 id="context-budget-title">预算</h3><span>{pack.budget.token_estimator}</span></div>
        <div className="context-budget-grid">
          <div>
            <span>字符</span><strong>{formatNumber(pack.budget.used_characters)} / {formatNumber(pack.budget.max_characters)}</strong>
            <progress aria-label="字符预算" max={pack.budget.max_characters} value={Math.min(pack.budget.used_characters, pack.budget.max_characters)} />
            <small>{budgetPercent(pack.budget.used_characters, pack.budget.max_characters)}% · 剩余 {formatNumber(pack.budget.remaining_characters)}{pack.budget.overflow_characters > 0 ? ` · 超出 ${formatNumber(pack.budget.overflow_characters)}` : ''}</small>
          </div>
          <div>
            <span>Token</span><strong>{formatNumber(pack.budget.used_tokens)} / {formatNumber(pack.budget.max_tokens)}</strong>
            <progress aria-label="Token 预算" max={pack.budget.max_tokens} value={Math.min(pack.budget.used_tokens, pack.budget.max_tokens)} />
            <small>{budgetPercent(pack.budget.used_tokens, pack.budget.max_tokens)}% · 剩余 {formatNumber(pack.budget.remaining_tokens)}{pack.budget.overflow_tokens > 0 ? ` · 超出 ${formatNumber(pack.budget.overflow_tokens)}` : ''}</small>
          </div>
        </div>
      </section>

      <details className="context-disclosure" open>
        <summary><span>已纳入</span><b>{pack.included.length}</b></summary>
        <div className="context-fragment-list">
          {pack.included.length === 0 && <p className="context-empty-row">没有纳入片段</p>}
          {pack.included.map((fragment) => (
            <article className="context-fragment-row" key={fragment.source_id}>
              <FileSearch size={14} />
              <div>
                <strong>{fragment.title}</strong>
                <span>{fragment.source_type} · {fragment.authority} · {fragment.selection_reason}</span>
                <code>{fragment.source_id}</code>
                <KnowledgeProvenanceTrace
                  title={fragment.title}
                  provenance={fragment.knowledge_provenance}
                />
              </div>
              <small>{formatNumber(fragment.estimated_characters)} 字<br />{formatNumber(fragment.estimated_tokens)} tok</small>
            </article>
          ))}
        </div>
      </details>

      <details className="context-disclosure" open>
        <summary><span>未纳入</span><b>{pack.excluded.length}</b></summary>
        <div className="context-fragment-list">
          {pack.excluded.length === 0 && <p className="context-empty-row">没有排除片段</p>}
          {pack.excluded.map((fragment) => (
            <article className="context-fragment-row context-fragment-excluded" key={fragment.source_id}>
              <AlertTriangle size={14} />
              <div>
                <strong>{fragment.title}</strong>
                <span>{fragment.source_type} · {fragment.authority}</span>
                <code>{fragment.exclusion_reason}</code>
                <KnowledgeProvenanceTrace
                  title={fragment.title}
                  provenance={fragment.knowledge_provenance}
                />
              </div>
              <small>{formatNumber(fragment.estimated_characters)} 字<br />{formatNumber(fragment.estimated_tokens)} tok</small>
            </article>
          ))}
        </div>
      </details>

      <details className="context-disclosure" open>
        <summary><span>Gate</span><b>{blockingGates} 阻断 · {warningGates} 警告</b></summary>
        <div className="context-gate-list">
          {pack.gates.length === 0 && <p className="context-empty-row">没有 Gate</p>}
          {pack.gates.map((gate) => (
            <div className={`context-gate-row gate-${gate.severity}`} key={`${gate.code}:${gate.source_id ?? ''}`}>
              {gate.severity === 'blocking' ? <AlertTriangle size={14} /> : <CircleDot size={14} />}
              <div><strong>{gate.code}</strong><span>{gate.message}</span>{gate.source_id && <code>{gate.source_id}</code>}</div>
            </div>
          ))}
        </div>
      </details>

      <details className="context-disclosure provider-message-disclosure" open>
        <summary><span>实际发送给模型的消息</span><b>{pack.exact_provider_messages.length}</b></summary>
        <div className="provider-message-list">
          {pack.exact_provider_messages.map((message, index) => (
            <article key={`${message.role}-${index}`}>
              <strong>{index + 1}. {roleLabels[message.role]}</strong>
              <pre>{message.content}</pre>
            </article>
          ))}
        </div>
      </details>

      <details className="context-hashes" aria-labelledby="context-hashes-title">
        <summary><h3 id="context-hashes-title">完整性技术证据</h3><span>hash / receipt / id 可复制</span></summary>
        <dl>
          <div><dt>pack_hash</dt><dd><TechValue label="pack hash" value={pack.pack_hash} /></dd></div>
          <div><dt>injection_hash</dt><dd><TechValue label="injection hash" value={pack.injection_hash} /></dd></div>
          <div><dt>provider_messages_hash</dt><dd><TechValue label="provider messages hash" value={pack.provider_messages_hash} /></dd></div>
          {nlclRoute && <div><dt>nlcl_receipt_hash</dt><dd><TechValue label="nlcl receipt hash" value={nlclRoute.receipt_hash} /></dd></div>}
          {operationPackage && <div><dt>prewrite_package_hash</dt><dd><TechValue label="prewrite package hash" value={operationPackage.package_hash} /></dd></div>}
          {routeExecutions.map((execution) => execution.receipt && (
            <div key={execution.call_ordinal}><dt>nlcl_execution_{execution.call_ordinal}_hash</dt><dd><TechValue label={`nlcl execution ${execution.call_ordinal} hash`} value={execution.receipt.receipt_hash} /></dd></div>
          ))}
        </dl>
      </details>

      {!agentAvailable && <div className="context-agent-unavailable" role="status"><CircleDot size={14} /><span>{agentDetail}</span></div>}
      {runActive && <div className="context-agent-unavailable" role="status"><CircleDot size={14} /><span>当前 Run 尚未结束，结束后才能确认此 Pack</span></div>}
      {error && <div className="context-agent-unavailable" role="alert"><AlertTriangle size={14} /><span>{error}</span></div>}

      {!readOnly && <section className="writer-mode-control" aria-labelledby="writer-mode-title">
        <div>
          <strong id="writer-mode-title">Writer 输出</strong>
          <span>{writerMode === 'author_review_change_proposal' ? '生成正文改动候选，仍需作者审阅后写回' : '只生成修复方案，不创建正文改动'}</span>
        </div>
        <div className="writer-mode-segments" role="radiogroup" aria-label="Writer 输出模式">
          <button type="button" role="radio" aria-checked={writerMode === 'repair_only'} className={writerMode === 'repair_only' ? 'is-active' : ''} disabled={operating} onClick={() => onWriterModeChange?.('repair_only')}>诊断修复</button>
          <button type="button" role="radio" aria-checked={writerMode === 'author_review_change_proposal'} className={writerMode === 'author_review_change_proposal' ? 'is-active' : ''} disabled={operating || pack.active_chapter_id === null} onClick={() => onWriterModeChange?.('author_review_change_proposal')}>正文候选</button>
        </div>
      </section>}

      {!readOnly && <footer className="context-pack-actions">
        <button type="button" className="quiet-button" disabled={operating} onClick={onDiscard}><Trash2 size={14} />放弃</button>
        <button type="button" className="quiet-button" disabled={operating || runActive} onClick={() => void onRefresh?.().catch(() => undefined)}><RefreshCw size={14} />重新编译</button>
        <button type="button" className="full-action context-confirm" disabled={confirmDisabled} onClick={() => void onConfirm?.().catch(() => undefined)}>{state === 'confirming' ? <LoaderCircle size={15} className="spin" /> : <Play size={15} />}{writerMode === 'author_review_change_proposal' ? '确认并生成候选' : '确认并运行'}</button>
      </footer>}
    </div>
  )
}

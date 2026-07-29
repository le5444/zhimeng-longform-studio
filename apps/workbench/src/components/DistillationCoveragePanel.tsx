import {
  AlertTriangle,
  BookOpenCheck,
  CheckCircle2,
  Database,
  LoaderCircle,
  RefreshCw,
  Search,
  ShieldAlert,
} from 'lucide-react'
import type {
  DistillationCardType,
  DistillationHealth,
  DistillationHealthState,
  DistillationProfile,
} from '../api/distillation'
import { useDistillationPreview } from '../hooks/useDistillationPreview'

const profileOptions: Array<{ value: DistillationProfile; label: string }> = [
  { value: 'draft_assist', label: '轻量起草' },
  { value: 'chapter', label: '章节控制' },
  { value: 'scene', label: '场景' },
  { value: 'review', label: '审稿' },
  { value: 'rewrite', label: '定向返工' },
  { value: 'deai', label: '去 AI 味' },
  { value: 'style_calibration', label: '文风校准' },
  { value: 'story_unit', label: '故事单元' },
  { value: 'debt_recall', label: '伏笔债务' },
  { value: 'volume', label: '卷级规划' },
  { value: 'packaging', label: '包装' },
  { value: 'benchmark', label: '明确对标' },
  { value: 'mechanism_transfer', label: '跨题材机制' },
]

const targetHealthLabels: Record<DistillationHealthState, string> = {
  healthy: '可用',
  degraded: '风险预览',
  blocked: '阻断',
}

function libraryHealthLabel(health: DistillationHealth): string {
  if (health.health === 'healthy') return '健康'
  if (health.health === 'blocked') return '阻断'
  if (health.validation_maturity === 'risk_ready' && health.structural_health === 'healthy') {
    return '风险就绪'
  }
  if (health.validation_maturity === 'degraded') return '验证退化'
  return '结构退化'
}

function percent(value: number): string {
  return `${Math.round(Math.max(0, Math.min(1, value)) * 100)}%`
}

function sumCounts(gaps: Partial<Record<DistillationCardType, number>>): number {
  return Object.values(gaps).reduce((total, value) => total + (value ?? 0), 0)
}

function compactFingerprint(value: string): string {
  return value.replace(/^sha256:/, '').slice(0, 8)
}

export function DistillationCoveragePanel({ enabled = true }: { enabled?: boolean }) {
  const preview = useDistillationPreview(enabled)
  const busy = preview.healthState === 'loading' || preview.coverageState === 'loading'
  const coverage = preview.coverage
  const riskCount = coverage
    ? sumCounts(coverage.selected_risk_ready_type_counts)
    : 0
  const hardRuleCount = coverage
    ? sumCounts(coverage.selected_hard_rule_type_counts)
    : 0

  if (!enabled) {
    return (
      <div className="inspector-body distillation-panel" role="tabpanel" id="inspector-panel-context" aria-labelledby="inspector-tab-context">
        <div className="distillation-empty"><AlertTriangle size={16} /><span>Writing Runtime 未就绪</span></div>
      </div>
    )
  }

  return (
    <div className="inspector-body distillation-panel" role="tabpanel" id="inspector-panel-context" aria-labelledby="inspector-tab-context" aria-busy={busy}>
      <header className="distillation-healthbar">
        <span className={`distillation-health-icon health-${preview.health?.health ?? 'loading'}`}>
          {preview.healthState === 'loading'
            ? <LoaderCircle size={15} className="spin" />
            : preview.health?.health === 'healthy'
              ? <CheckCircle2 size={15} />
              : <Database size={15} />}
        </span>
        <div>
          <strong>蒸馏库</strong>
          <span>
            {preview.healthState === 'loading'
              ? '正在读取健康状态'
              : preview.health
                ? `${libraryHealthLabel(preview.health)} · ${preview.health.card_count.toLocaleString()} 张卡`
                : preview.healthError
                  ? <span role="alert">{preview.healthError}</span>
                  : '健康状态不可用'}
          </span>
        </div>
        <button type="button" className="icon-button" aria-label="刷新蒸馏库健康状态" title="刷新" disabled={preview.healthState === 'loading' || preview.coverageState === 'loading'} onClick={() => void preview.refreshHealth()}><RefreshCw size={14} /></button>
      </header>

      <form
        className="coverage-form"
        onSubmit={(event) => {
          event.preventDefault()
          void preview.runCoverage()
        }}
      >
        <label htmlFor="distillation-query">写作目标</label>
        <textarea
          id="distillation-query"
          maxLength={1_000}
          placeholder="例如：番茄男频玄幻第一章，金手指首次兑现，爽点前置"
          value={preview.query}
          disabled={busy}
          onChange={(event) => preview.setQuery(event.target.value)}
        />
        <div className="coverage-form-actions">
          <label htmlFor="distillation-profile" className="sr-only">任务类型</label>
          <select
            id="distillation-profile"
            value={preview.profile}
            disabled={busy}
            onChange={(event) => preview.setProfile(event.target.value as DistillationProfile)}
          >
            {profileOptions.map((option) => <option key={option.value} value={option.value}>{option.label}</option>)}
          </select>
          <button type="submit" disabled={!preview.query.trim() || busy || preview.healthState !== 'ready'}>
            {preview.coverageState === 'loading' ? <LoaderCircle size={14} className="spin" /> : <Search size={14} />}
            {preview.coverageState === 'loading' ? '检查中' : '检查覆盖'}
          </button>
        </div>
      </form>

      {preview.coverageState === 'loading' && (
        <div className="coverage-running" role="status"><LoaderCircle size={16} className="spin" /><div><strong>正在检索正式库</strong><span>通常需要 20-30 秒</span></div></div>
      )}
      {preview.coverageError && (
        <div className="coverage-error" role="alert"><AlertTriangle size={15} /><span>{preview.coverageError}</span></div>
      )}

      {coverage && (
        <>
          <section className={`coverage-verdict verdict-${coverage.target_health}`} role="status" aria-live="polite">
            {coverage.target_health === 'healthy' ? <BookOpenCheck size={17} /> : <ShieldAlert size={17} />}
            <div>
              <strong>{targetHealthLabels[coverage.target_health]}</strong>
              <span>{coverage.formal_injection_allowed ? '覆盖满足后续装配条件' : coverage.risk_preview_allowed ? '只能作为风险预览，不进入正式写作' : '当前目标缺少必要来源'}</span>
            </div>
            <small>只读</small>
          </section>

          <section className="metric-strip coverage-metrics" aria-label="蒸馏覆盖率">
            <div><span>来源覆盖</span><strong>{percent(coverage.source_coverage_ratio)}</strong></div>
            <div><span>正式覆盖</span><strong>{percent(coverage.formal_coverage_ratio)}</strong></div>
            <div><span>验证缺口</span><strong>{sumCounts(coverage.formal_quota_gaps)}</strong></div>
          </section>

          <div className="coverage-summary-line">
            <span><b>{hardRuleCount}</b> hard-rule</span>
            <span><b>{riskCount}</b> risk-ready</span>
            <span title={coverage.library_fingerprint}>库 {compactFingerprint(coverage.library_fingerprint)}</span>
          </div>

          {coverage.feed_request && (
            <section className="coverage-guidance"><AlertTriangle size={15} /><span>{coverage.feed_request}</span></section>
          )}

          <div className="section-title coverage-section-title"><div><h3>匹配卡</h3><span>{coverage.selected_cards.length} / {coverage.available_matched_cards}</span></div></div>
          <div className="coverage-card-list">
            {coverage.selected_cards.map((card) => (
              <article className="coverage-card-row" key={card.card_id}>
                <div className="coverage-card-heading">
                  <span className={`usage-mark usage-${card.usage_mode}`}>{card.usage_mode === 'hard_rule' ? 'HARD' : 'RISK'}</span>
                  <span>{card.card_type}</span>
                </div>
                <strong>{card.title}</strong>
                <p>{card.summary}</p>
                {card.tags.length > 0 && <div className="coverage-tags">{card.tags.slice(0, 4).map((tag) => <span key={tag}>{tag}</span>)}</div>}
              </article>
            ))}
          </div>
        </>
      )}
    </div>
  )
}

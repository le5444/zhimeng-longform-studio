import {
  AlertTriangle,
  CheckCircle2,
  FileCode2,
  LoaderCircle,
  PackagePlus,
  RefreshCw,
  ShieldCheck,
} from 'lucide-react'
import { useSkillAdaptation } from '../hooks/useSkillAdaptation'

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
}

export function SkillAdaptationPanel({
  enabled = true,
  onImported,
}: {
  enabled?: boolean
  onImported?: () => void | Promise<void>
}) {
  const adaptation = useSkillAdaptation(enabled)
  const plan = adaptation.plan
  const materialization = adaptation.materialization

  return (
    <section className="managed-skill-panel skill-handoff-panel" aria-label="Raw Skill 适配">
      <header className={`skill-handoff-hero status-${adaptation.state === 'error' ? 'failed' : materialization?.imported ? 'succeeded' : 'prepared'}`}>
        <span className="skill-handoff-icon">
          {adaptation.busy
            ? <LoaderCircle size={16} className="spin" />
            : materialization?.imported
              ? <CheckCircle2 size={16} />
              : <FileCode2 size={16} />}
        </span>
        <div>
          <span className="panel-kicker">M3x Raw Skill 适配</span>
          <h3>
            {materialization?.imported
              ? '已物化并 inert 导入'
              : plan
                ? '适配计划已就绪'
                : '把 raw SKILL.md 适配为 instruction_source'}
          </h3>
          <p>
            只做 plan → materialize → Runtime-owned inert import。不执行 raw SKILL.md，不自动生成 deterministic tool，formal_draft_allowed 保持 false。
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新适配计划"
          disabled={!plan || adaptation.busy}
          onClick={() => void adaptation.refreshPlan()}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="skill-handoff-flow" aria-label="适配路径">
        <div><strong>trusted raw path</strong><span>SKILL.md + frontmatter</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>适配计划</strong><span>缺件清单 / not_claiming</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>物化包</strong><span>manifest + instruction-map</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>inert 导入</strong><span>作者再手动启用</span></div>
      </div>

      {!enabled && (
        <p className="skill-handoff-empty">Writing Runtime 未就绪，无法适配 raw skill。</p>
      )}

      {enabled && (
        <form
          className="coverage-form skill-adaptation-form"
          onSubmit={(event) => {
            event.preventDefault()
            void adaptation.planFromPath()
          }}
        >
          <label htmlFor="raw-skill-source-path">受信任 raw skill 路径</label>
          <textarea
            id="raw-skill-source-path"
            maxLength={4096}
            placeholder="例如 C:\skills\sample-volume-folder 或 ...\SKILL.md"
            value={adaptation.sourcePath}
            disabled={adaptation.busy}
            onChange={(event) => adaptation.setSourcePath(event.target.value)}
          />
          <div className="coverage-form-actions">
            <button type="submit" disabled={adaptation.busy || !adaptation.sourcePath.trim()}>
              {adaptation.busy && !plan ? <LoaderCircle size={14} className="spin" /> : <ShieldCheck size={14} />}
              <span>{adaptation.busy && !plan ? '规划中' : '生成适配计划'}</span>
            </button>
            <button
              type="button"
              disabled={!plan || adaptation.busy}
              onClick={() => {
                void adaptation.materialize(true).then(() => {
                  void onImported?.()
                }).catch(() => undefined)
              }}
            >
              {adaptation.busy && plan ? <LoaderCircle size={14} className="spin" /> : <PackagePlus size={14} />}
              <span>{adaptation.busy && plan ? '物化中' : '物化并导入'}</span>
            </button>
          </div>
        </form>
      )}

      {adaptation.error && (
        <div className="skill-handoff-error" role="alert">
          <AlertTriangle size={15} />
          <span>{adaptation.error}</span>
        </div>
      )}

      {plan && (
        <div className="skill-handoff-details" aria-label="适配计划详情">
          <dl>
            <div><dt>plan_id</dt><dd><code>{plan.plan_id}</code></dd></div>
            <div><dt>plan_hash</dt><dd><code>{shortHash(plan.plan_hash)}</code></dd></div>
            <div><dt>target skill</dt><dd><code>{plan.target.skill_id}</code></dd></div>
            <div><dt>kind</dt><dd><code>{plan.target.kind}</code></dd></div>
            <div><dt>executable</dt><dd><code>{String(plan.target.executable)}</code></dd></div>
            <div><dt>formal_draft_allowed</dt><dd><code>{String(plan.target.formal_draft_allowed)}</code></dd></div>
            <div><dt>source name</dt><dd><code>{plan.source.frontmatter_name}</code></dd></div>
            <div><dt>skill.md hash</dt><dd><code>{shortHash(plan.source.skill_md_hash)}</code></dd></div>
          </dl>
          <section>
            <h4>缺件 / 说明</h4>
            <ul>
              {plan.missing.map((item) => (
                <li key={item.code}><code>{item.code}</code> {item.message}</li>
              ))}
            </ul>
          </section>
          <section>
            <h4>not_claiming</h4>
            <ul>
              {plan.not_claiming.map((item) => (
                <li key={item}><code>{item}</code></li>
              ))}
            </ul>
          </section>
        </div>
      )}

      {materialization && (
        <div className="skill-handoff-details" aria-label="物化结果">
          <dl>
            <div><dt>materialization_id</dt><dd><code>{materialization.materialization_id}</code></dd></div>
            <div><dt>package_root</dt><dd><code>{materialization.package_root}</code></dd></div>
            <div><dt>package_fingerprint</dt><dd><code>{shortHash(materialization.package_fingerprint)}</code></dd></div>
            <div><dt>imported</dt><dd><code>{String(materialization.imported)}</code></dd></div>
            <div><dt>skill_version_id</dt><dd><code>{materialization.skill_version_id ?? '未导入'}</code></dd></div>
            <div><dt>formal_draft_allowed</dt><dd><code>{String(materialization.formal_draft_allowed)}</code></dd></div>
          </dl>
          {materialization.imported && (
            <p className="skill-handoff-empty">
              已 inert 导入。请到上方生命周期面板手动 enable 后，才会进入项目可路由 catalog。
            </p>
          )}
        </div>
      )}
    </section>
  )
}

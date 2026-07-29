import {
  AlertTriangle,
  BadgeCheck,
  CheckCircle2,
  LoaderCircle,
  LockKeyhole,
  Power,
  PowerOff,
  RefreshCw,
  ShieldCheck,
  Undo2,
} from 'lucide-react'
import { useManagedSkills } from '../hooks/useManagedSkills'
import type { ManagedSkillRow } from '../hooks/useManagedSkills'

interface ManagedSkillLifecyclePanelProps {
  enabled: boolean
  projectId: string | null
}

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未绑定'
}

function configuredLabel(state: ManagedSkillRow['activation']['configured_state']): string {
  if (state === 'enabled') return '项目已启用'
  if (state === 'disabled') return '项目已禁用'
  if (state === 'inherited') return '继承应用'
  return '未配置'
}

function effectiveLabel(row: ManagedSkillRow): string {
  if (row.activation.effective_state === 'enabled') {
    return row.activation.effective_scope === 'project' ? '项目生效中' : '应用生效中'
  }
  return '当前不可路由'
}

export function ManagedSkillLifecyclePanel({
  enabled,
  projectId,
}: ManagedSkillLifecyclePanelProps) {
  const lifecycle = useManagedSkills(enabled, projectId)
  const ready = enabled && Boolean(projectId)
  const loading = lifecycle.state === 'loading'
  const empty = lifecycle.state === 'ready' && lifecycle.rows.length === 0

  return (
    <section className="managed-skill-panel skill-handoff-panel" aria-label="受管 Skill 生命周期">
      <header className={`skill-handoff-hero status-${lifecycle.state === 'error' ? 'failed' : 'prepared'}`}>
        <span className="skill-handoff-icon">
          {loading || lifecycle.busySkillId
            ? <LoaderCircle size={16} className="spin" />
            : lifecycle.state === 'error'
              ? <AlertTriangle size={16} />
              : <BadgeCheck size={16} />}
        </span>
        <div>
          <span className="panel-kicker">M3l 受管 Skill 生命周期</span>
          <h3>{ready ? '项目级启用 / 禁用' : '等待项目与 Runtime'}</h3>
          <p>
            只展示 Runtime-owned snapshot 与 CAS 激活状态。这里不写正文，也不声称通用四 Skills 编排完成。
          </p>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="刷新受管 Skill 生命周期"
          onClick={() => void lifecycle.refresh()}
          disabled={!ready || Boolean(lifecycle.busySkillId)}
        >
          <RefreshCw size={14} />
        </button>
      </header>

      <div className="skill-handoff-flow" aria-label="生命周期路径">
        <div><strong>受管 snapshot</strong><span>内容寻址包，不读用户级路径</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>作者 CAS 激活</strong><span>enable / disable / inherit</span></div>
        <i aria-hidden="true">→</i>
        <div><strong>有效 catalog</strong><span>Agent / Workflow 只走生效版本</span></div>
      </div>

      {lifecycle.error && (
        <div className="skill-handoff-error" role="alert">
          <AlertTriangle size={15} />
          <span>{lifecycle.error}</span>
        </div>
      )}

      {!ready && (
        <p className="skill-handoff-empty">需要 Runtime 就绪并绑定项目后，才能读取项目级 Skill 生命周期。</p>
      )}

      {ready && loading && (
        <p className="skill-handoff-empty">正在读取项目级 managed-skills catalog…</p>
      )}

      {ready && empty && (
        <p className="skill-handoff-empty">当前项目还没有受管 Skill 激活记录。</p>
      )}

      {ready && lifecycle.rows.length > 0 && (
        <div className="managed-skill-list" aria-label="项目受管 Skill 列表">
          {lifecycle.rows.map((row) => {
            const busy = lifecycle.busySkillId === row.activation.skill_id
            const version = row.preferredVersion
            const canEnable = !busy && (
              row.activation.configured_state !== 'enabled'
              || row.activation.effective_state !== 'enabled'
              || row.activation.effective_scope !== 'project'
            ) && Boolean(row.preferredVersion || row.activation.effective_version_id)
            const canDisable = !busy && row.activation.effective_state === 'enabled'
            const canInherit = !busy && (
              row.activation.configured_state === 'enabled'
              || row.activation.configured_state === 'disabled'
            )
            return (
              <article
                key={row.activation.skill_id}
                className={`managed-skill-card state-${row.activation.effective_state}`}
                data-skill-id={row.activation.skill_id}
                data-effective-state={row.activation.effective_state}
                data-configured-state={row.activation.configured_state}
              >
                <div className="managed-skill-card-head">
                  <span className="managed-skill-kind">
                    {version?.manifest.kind === 'instruction_source' ? '指令源' : '确定性工具'}
                  </span>
                  <strong>{version?.manifest.display_name ?? row.activation.skill_id}</strong>
                  <code>{row.activation.skill_id}</code>
                </div>
                <div className="managed-skill-meta">
                  <span>配置：{configuredLabel(row.activation.configured_state)}</span>
                  <span>有效：{effectiveLabel(row)}</span>
                  <span>revision：{row.activation.state_revision}</span>
                  <span>版本：{version?.manifest.version ?? '未绑定'}</span>
                </div>
                <p>{version?.manifest.description ?? '暂无描述'}</p>
                <div className="managed-skill-tech">
                  <span>version_id {shortHash(row.activation.effective_version_id ?? row.activation.configured_version_id)}</span>
                  <span>package {shortHash(version?.package_fingerprint)}</span>
                  <span>source {version?.source.kind ?? 'unknown'}</span>
                </div>
                <div className="managed-skill-actions">
                  <button
                    type="button"
                    className="quiet-button"
                    disabled={!canEnable}
                    onClick={() => void lifecycle.enable(row.activation.skill_id)}
                  >
                    {busy ? <LoaderCircle size={14} className="spin" /> : <Power size={14} />}
                    项目启用
                  </button>
                  <button
                    type="button"
                    className="quiet-button"
                    disabled={!canDisable}
                    onClick={() => void lifecycle.disable(row.activation.skill_id)}
                  >
                    <PowerOff size={14} />
                    项目禁用
                  </button>
                  <button
                    type="button"
                    className="quiet-button"
                    disabled={!canInherit}
                    onClick={() => void lifecycle.inherit(row.activation.skill_id)}
                  >
                    <Undo2 size={14} />
                    恢复继承
                  </button>
                </div>
              </article>
            )
          })}
        </div>
      )}

      <div className="skill-handoff-permissions" aria-label="生命周期边界">
        <div>
          <LockKeyhole size={14} />
          <strong>CAS</strong>
          <span>启用/禁用携带 expected_state_revision 与 prior version，冲突时 fail closed。</span>
        </div>
        <div>
          <ShieldCheck size={14} />
          <strong>边界</strong>
          <span>只管理已适配受管包；不导入原始 SKILL.md，不授权 formal_draft。</span>
        </div>
        <div>
          <CheckCircle2 size={14} />
          <strong>执行入口</strong>
          <span>Agent / Workflow 只消费项目有效 catalog 中的生效 snapshot。</span>
        </div>
      </div>
    </section>
  )
}

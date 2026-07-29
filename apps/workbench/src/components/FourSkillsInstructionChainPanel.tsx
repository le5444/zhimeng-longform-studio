import {
  AlertTriangle,
  CheckCircle2,
  Link2,
  LoaderCircle,
  Route,
  ShieldAlert,
  ShieldCheck,
  Workflow,
} from 'lucide-react'
import { useFourSkillsInstructionChain } from '../hooks/useFourSkillsInstructionChain'
import type { FourSkillsFamily } from '../api/fourSkillsRawAdaptations'

const FAMILIES: FourSkillsFamily[] = [
  'source_ingest',
  'distillation',
  'knowledge_context',
  'creation_suite',
]

function shortHash(value: string | null | undefined): string {
  return value ? `${value.slice(0, 8)}…${value.slice(-6)}` : '未生成'
}

function stageClass(done: boolean, active: boolean): string {
  if (done) return 'is-done'
  if (active) return 'is-active'
  return 'is-pending'
}

export function FourSkillsInstructionChainPanel({
  enabled = true,
  projectId = null,
  projectRevision = null,
  projectFingerprint = null,
  onImported,
}: {
  enabled?: boolean
  projectId?: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  onImported?: () => void | Promise<void>
}) {
  const chain = useFourSkillsInstructionChain(
    enabled,
    projectId,
    projectRevision,
    projectFingerprint,
  )
  const handoffReady = Boolean(chain.handoff?.instruction_chain_ready)
  const probeComplete = Boolean(chain.executionProbe?.probe_complete)
  const managedDone = Boolean(chain.managedExecution?.status === 'complete')
  const managedOnce = Boolean(chain.managedExecution?.four_skills_generic_exactly_once)
  const projectBound = Boolean(chain.projectBinding?.status === 'bound')
  const orchestrated = Boolean(chain.orchestration?.orchestration_ready)
  const bound = Boolean(chain.binding?.workflow_ready)
  const materialized = Boolean(chain.batch?.authorization_input_ready)

  return (
    <section className="managed-skill-panel skill-handoff-panel four-skills-chain-panel" aria-label="四 Skills Instruction 全链">
      <header className={`skill-handoff-hero status-${chain.state === 'error' ? 'failed' : handoffReady ? 'succeeded' : 'prepared'}`}>
        <span className="skill-handoff-icon">
          {chain.busy
            ? <LoaderCircle size={16} className="spin" />
            : handoffReady
              ? <CheckCircle2 size={16} />
              : <Workflow size={16} />}
        </span>
        <div>
          <span className="panel-kicker">M3an Instruction 全链交接</span>
          <h3>
            {managedOnce
              ? '家族 managed 执行已 exactly-once（raw 仍未执行，正式正文仍锁）'
              : projectBound
                ? 'Instruction 链已项目绑定'
                : handoffReady
                  ? 'Instruction 链已交接；探测后可跑家族 managed 执行'
                  : orchestrated
                    ? '编排完成，可交接'
                    : '四家族 raw → 绑定 → 编排 → 交接 → 探测 → managed 执行 → 项目绑定'}
          </h3>
          <p>
            作者显式推进 plan / materialize / bind / orchestrate / handoff / probe / managed-exec / project-bind。
            raw_skill_md_execution 与 formal_draft_allowed 固定 false；managed 执行仅作用于家族确定性工具。
          </p>
        </div>
      </header>

      <div className="four-skills-stage-rail" aria-label="Instruction 链阶段">
        <div className={stageClass(materialized, chain.busy && !materialized)}>
          <ShieldCheck size={13} />
          <strong>适配物化</strong>
          <span>{materialized ? 'ready' : 'pending'}</span>
        </div>
        <i aria-hidden="true">→</i>
        <div className={stageClass(bound, materialized && !bound)}>
          <Link2 size={13} />
          <strong>workflow 绑定</strong>
          <span>{bound ? 'bound' : 'pending'}</span>
        </div>
        <i aria-hidden="true">→</i>
        <div className={stageClass(orchestrated, bound && !orchestrated)}>
          <Route size={13} />
          <strong>instruction 编排</strong>
          <span>{orchestrated ? 'completed' : 'pending'}</span>
        </div>
        <i aria-hidden="true">→</i>
        <div className={stageClass(handoffReady, orchestrated && !handoffReady)}>
          <Workflow size={13} />
          <strong>链交接</strong>
          <span>{handoffReady ? 'ready' : 'pending'}</span>
        </div>
        <i aria-hidden="true">→</i>
        <div className={stageClass(probeComplete, handoffReady && !probeComplete)}>
          <ShieldAlert size={13} />
          <strong>执行探测</strong>
          <span>{probeComplete ? 'denied' : 'pending'}</span>
        </div>
        <i aria-hidden="true">→</i>
        <div className={stageClass(managedDone, probeComplete && !managedDone)}>
          <ShieldCheck size={13} />
          <strong>managed 执行</strong>
          <span>{managedOnce ? 'exactly-once' : managedDone ? 'complete' : 'pending'}</span>
        </div>
      </div>

      {!enabled && (
        <p className="skill-handoff-empty">Writing Runtime 未就绪，无法跑四 Skills Instruction 链。</p>
      )}

      {enabled && (
        <form
          className="coverage-form four-skills-chain-form"
          onSubmit={(event) => {
            event.preventDefault()
            void chain.runFullChain().then(() => {
              void onImported?.()
            }).catch(() => undefined)
          }}
        >
          <div className="four-skills-path-grid">
            {FAMILIES.map((family) => (
              <label key={family} htmlFor={`four-skills-path-${family}`}>
                <span>{chain.familyLabels[family]}</span>
                <input
                  id={`four-skills-path-${family}`}
                  maxLength={4096}
                  placeholder={`${family} 的受信任 raw skill 路径`}
                  value={chain.paths[family]}
                  disabled={chain.busy}
                  onChange={(event) => chain.setPath(family, event.target.value)}
                />
              </label>
            ))}
          </div>
          <div className="coverage-form-actions">
            <button type="submit" disabled={chain.busy || !chain.membersReady}>
              {chain.busy ? <LoaderCircle size={14} className="spin" /> : <Workflow size={14} />}
              <span>{chain.busy ? '推进中' : '跑完整 Instruction 链'}</span>
            </button>
          </div>
        </form>
      )}

      {chain.error && (
        <div className="skill-handoff-error" role="alert">
          <AlertTriangle size={15} />
          <span>{chain.error}</span>
        </div>
      )}

      {(chain.batch || chain.binding || chain.orchestration || chain.handoff || chain.executionProbe || chain.managedExecution) && (
        <div className="skill-handoff-details" aria-label="Instruction 链收据">
          <dl>
            <div><dt>batch_id</dt><dd><code>{chain.batch?.batch_id ?? '—'}</code></dd></div>
            <div><dt>batch_hash</dt><dd><code>{shortHash(chain.batch?.batch_hash)}</code></dd></div>
            <div><dt>binding_id</dt><dd><code>{chain.binding?.binding_id ?? '—'}</code></dd></div>
            <div><dt>orchestration_id</dt><dd><code>{chain.orchestration?.orchestration_id ?? '—'}</code></dd></div>
            <div><dt>probe_id</dt><dd><code>{chain.executionProbe?.probe_id ?? '—'}</code></dd></div>
            <div><dt>generic_execution_ready</dt><dd><code>{String(chain.executionProbe?.generic_execution_ready ?? false)}</code></dd></div>
            <div><dt>denied_step_count</dt><dd><code>{chain.executionProbe?.denied_step_count ?? '—'}</code></dd></div>
            <div><dt>execution_id</dt><dd><code>{chain.managedExecution?.execution_id ?? '—'}</code></dd></div>
            <div><dt>managed_succeeded</dt><dd><code>{chain.managedExecution?.succeeded_step_count ?? '—'}/4</code></dd></div>
            <div><dt>four_skills_generic_exactly_once</dt><dd><code>{String(chain.managedExecution?.four_skills_generic_exactly_once ?? false)}</code></dd></div>
            <div><dt>handoff_id</dt><dd><code>{chain.handoff?.handoff_id ?? '—'}</code></dd></div>
            <div><dt>project_binding_id</dt><dd><code>{chain.projectBinding?.binding_id ?? '—'}</code></dd></div>
            <div><dt>instruction_chain_ready</dt><dd><code>{String(chain.handoff?.instruction_chain_ready ?? false)}</code></dd></div>
            <div><dt>project_bound</dt><dd><code>{String(projectBound)}</code></dd></div>
            <div><dt>managed_skill_execution</dt><dd><code>{String(chain.managedExecution?.managed_skill_execution ?? false)}</code></dd></div>
            <div><dt>raw_skill_md_execution</dt><dd><code>false</code></dd></div>
            <div><dt>formal_draft_allowed</dt><dd><code>false</code></dd></div>
          </dl>

          {chain.handoff && (
            <section aria-label="交接步骤">
              <h4>交接步骤</h4>
              <ul className="managed-skill-chain-steps four-skills-handoff-steps">
                {chain.handoff.steps.map((step) => (
                  <li key={`${step.family}-${step.ordinal}`}>
                    <strong>{step.ordinal}. {chain.familyLabels[step.family]}</strong>
                    <span>{step.skill_id}</span>
                    <span>{step.status}</span>
                    <code>{shortHash(step.package_fingerprint)}</code>
                    <em>{step.detail}</em>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {chain.managedExecution && (
            <section aria-label="managed 执行步骤">
              <h4>家族 managed 执行</h4>
              <ul className="managed-skill-chain-steps four-skills-handoff-steps">
                {chain.managedExecution.steps.map((step) => (
                  <li key={`managed-${step.family}-${step.ordinal}`}>
                    <strong>{step.ordinal}. {chain.familyLabels[step.family]}</strong>
                    <span>{step.managed_skill_id}</span>
                    <span>{step.status}</span>
                    <code>{step.skill_receipt_id ? shortHash(step.skill_receipt_id) : '—'}</code>
                    <em>{step.detail}</em>
                  </li>
                ))}
              </ul>
            </section>
          )}

          {chain.notClaiming.length > 0 && (
            <section aria-label="不宣称范围">
              <h4>not_claiming</h4>
              <ul>
                {chain.notClaiming.map((item) => (
                  <li key={item}><code>{item}</code></li>
                ))}
              </ul>
            </section>
          )}
        </div>
      )}
    </section>
  )
}

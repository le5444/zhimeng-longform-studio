import {
  BookOpenCheck,
  Layers3,
  LockKeyhole,
  ShieldAlert,
  ShieldCheck,
  Sparkles,
  Users,
} from 'lucide-react'
import { useMemo, useState } from 'react'
import { ExternalBlindReviewMatrixPanel } from './ExternalBlindReviewMatrixPanel'
import { FormalDraftAuthorizationPanel } from './FormalDraftAuthorizationPanel'
import { GatePipelinePanel } from './GatePipelinePanel'
import { HardFactGatePanel } from './HardFactGatePanel'
import { LiteraryQualityGatePanel } from './LiteraryQualityGatePanel'
import { LlmLiteraryScorePanel } from './LlmLiteraryScorePanel'

export type GateWorkbenchStage =
  | 'pipeline'
  | 'literary'
  | 'llm_score'
  | 'blind_matrix'
  | 'hard_fact'
  | 'formal_auth'
  | 'all'

const STAGES: Array<{
  id: Exclude<GateWorkbenchStage, 'all'>
  label: string
  hint: string
  icon: typeof ShieldCheck
}> = [
  { id: 'pipeline', label: 'Pipeline', hint: '有序 Gate 链', icon: Layers3 },
  { id: 'literary', label: '文学质量', hint: 'advisory rubric', icon: BookOpenCheck },
  { id: 'llm_score', label: 'LLM 形分', hint: 'offline stub only', icon: Sparkles },
  { id: 'blind_matrix', label: '外部盲评', hint: '6 格 fail-closed', icon: Users },
  { id: 'hard_fact', label: '硬事实', hint: '授权输入', icon: ShieldAlert },
  { id: 'formal_auth', label: '正式授权', hint: '始终 fail-closed', icon: LockKeyhole },
]

function stageClass(active: boolean): string {
  return active ? 'is-active' : 'is-pending'
}

export function GateWorkbenchSurface({
  enabled,
  projectId,
  projectRevision = null,
  projectFingerprint = null,
  changeSetId,
  reviewRevision = null,
  changeSetStatus = null,
}: {
  enabled: boolean
  projectId: string | null
  projectRevision?: number | null
  projectFingerprint?: string | null
  changeSetId: string | null
  reviewRevision?: number | null
  changeSetStatus?: string | null
}) {
  const [stage, setStage] = useState<GateWorkbenchStage>('pipeline')
  const shortId = useMemo(
    () => (changeSetId ? `${changeSetId.slice(0, 10)}…` : '未选择'),
    [changeSetId],
  )

  return (
    <section className="gate-workbench-surface" aria-label="Gate 工作面">
      <header className="gate-workbench-hero">
        <span className="gate-workbench-icon">
          <ShieldCheck size={16} />
        </span>
        <div>
          <span className="panel-kicker">FR-GATE · 统一工作面</span>
          <h3>Pipeline → 文学质量 → LLM 形分 → 外部盲评 → 硬事实 → 正式授权</h3>
          <p>
            当前 ChangeSet <code>{shortId}</code>
            {changeSetStatus ? ` · status=${changeSetStatus}` : ''}
            {reviewRevision != null ? ` · rev=${reviewRevision}` : ''}
            。固定 quality_assessed=false、formal_draft_allowed=false，不自动解锁正文。
          </p>
        </div>
        <button
          type="button"
          className={`quiet-button${stage === 'all' ? ' is-active' : ''}`}
          aria-pressed={stage === 'all'}
          onClick={() => setStage('all')}
        >
          全部
        </button>
      </header>

      <div className="gate-workbench-stage-rail" role="tablist" aria-label="Gate 阶段">
        {STAGES.map((item, index) => {
          const Icon = item.icon
          return (
            <div key={item.id} className="gate-workbench-stage-group">
              {index > 0 && <i aria-hidden="true">→</i>}
              <button
                type="button"
                role="tab"
                aria-selected={stage === item.id}
                className={stageClass(stage === 'all' || stage === item.id)}
                onClick={() => setStage(item.id)}
              >
                <Icon size={13} />
                <strong>{item.label}</strong>
                <span>{item.hint}</span>
              </button>
            </div>
          )
        })}
      </div>

      <div className="gate-workbench-panels knowledge-workflow-stack">
        {(stage === 'pipeline' || stage === 'all') && (
          <GatePipelinePanel
            enabled={enabled}
            changeSetId={changeSetId}
            reviewRevision={reviewRevision}
          />
        )}
        {(stage === 'literary' || stage === 'all') && (
          <LiteraryQualityGatePanel
            enabled={enabled}
            changeSetId={changeSetId}
            reviewRevision={reviewRevision}
          />
        )}
        {(stage === 'llm_score' || stage === 'all') && (
          <LlmLiteraryScorePanel
            enabled={enabled}
            changeSetId={changeSetId}
            reviewRevision={reviewRevision}
          />
        )}
        {(stage === 'blind_matrix' || stage === 'all') && (
          <ExternalBlindReviewMatrixPanel enabled={enabled} />
        )}
        {(stage === 'hard_fact' || stage === 'all') && (
          <HardFactGatePanel
            enabled={enabled}
            changeSetId={changeSetId}
            reviewRevision={reviewRevision}
          />
        )}
        {(stage === 'formal_auth' || stage === 'all') && (
          <FormalDraftAuthorizationPanel
            enabled={enabled}
            projectId={projectId}
            projectRevision={projectRevision}
            projectFingerprint={projectFingerprint}
            changeSetId={changeSetId}
          />
        )}
      </div>
    </section>
  )
}

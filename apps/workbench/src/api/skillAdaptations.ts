import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface SkillAdaptationPlanRequest {
  source_path: string
}

export interface SkillAdaptationMaterializeRequest {
  expected_plan_id: string
  expected_plan_hash: string
  import_after_materialize?: boolean
}

export interface SkillAdaptationSource {
  source_path: string
  skill_md_path: string
  skill_md_hash: string
  skill_md_bytes: number
  frontmatter_name: string
  frontmatter_description: string
  locator_hash: string
}

export interface SkillAdaptationTarget {
  skill_id: string
  display_name: string
  description: string
  kind: 'instruction_source'
  version: string
  capability: 'raw-skill.instruction-source.v1'
  resources: string[]
  formal_draft_allowed: false
  executable: false
}

export interface SkillAdaptationMissing {
  code: string
  message: string
}

export interface SkillAdaptationPlan {
  schema: 'zhimeng.skill-adaptation-plan.v1'
  plan_id: string
  adapter_id: 'zhimeng.raw-skill-instruction'
  adapter_version: '1'
  adapter_fingerprint: string
  status: 'ready_to_materialize'
  source: SkillAdaptationSource
  target: SkillAdaptationTarget
  missing: SkillAdaptationMissing[]
  not_claiming: string[]
  created_at: string
  plan_hash: string
}

export interface SkillAdaptationMaterialization {
  schema: 'zhimeng.skill-adaptation-materialization.v1'
  materialization_id: string
  plan_id: string
  plan_hash: string
  package_root: string
  package_fingerprint: string
  skill_id: string
  skill_version: string
  kind: 'instruction_source'
  formal_draft_allowed: false
  imported: boolean
  skill_version_id: string | null
  import_receipt_id: string | null
  not_claiming: string[]
  created_at: string
  materialization_hash: string
}

export interface SkillAdaptationPlanResponse {
  schema: 'zhimeng.skill-adaptation-plan-response.v1'
  plan: SkillAdaptationPlan
}

export interface SkillAdaptationMaterializationResponse {
  schema: 'zhimeng.skill-adaptation-materialization-response.v1'
  materialization: SkillAdaptationMaterialization
  import_result: Record<string, unknown> | null
}

function jsonPost(body: unknown): RequestInit {
  return {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  }
}

export function planSkillAdaptation(
  request: SkillAdaptationPlanRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<SkillAdaptationPlanResponse>(
    '/api/v1/managed-skills/adaptations/plan',
    options,
    jsonPost({ source_path: request.source_path }),
  )
}

export function getSkillAdaptationPlan(
  planId: string,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<SkillAdaptationPlanResponse>(
    `/api/v1/managed-skills/adaptations/${encodeURIComponent(planId)}`,
    options,
  )
}

export function materializeSkillAdaptation(
  request: SkillAdaptationMaterializeRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<SkillAdaptationMaterializationResponse>(
    '/api/v1/managed-skills/adaptations/materialize',
    options,
    jsonPost({
      expected_plan_id: request.expected_plan_id,
      expected_plan_hash: request.expected_plan_hash,
      import_after_materialize: request.import_after_materialize ?? true,
    }),
  )
}

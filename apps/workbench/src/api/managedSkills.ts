import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export type ManagedSkillScopeType = 'application' | 'project'
export type ManagedSkillConfiguredState =
  | 'unconfigured'
  | 'inherited'
  | 'enabled'
  | 'disabled'
export type ManagedSkillEffectiveState = 'enabled' | 'disabled'
export type ManagedSkillEffectiveScope = 'application' | 'project' | 'none'
export type ManagedSkillActivationAction =
  | 'enable'
  | 'disable'
  | 'upgrade'
  | 'rollback'
  | 'inherit'
export type ManagedSkillSourceKind = 'builtin_bundle' | 'trusted_import'
export type ManagedSkillKind = 'deterministic_tool' | 'instruction_source'

export interface ManagedSkillScope {
  scope_type: ManagedSkillScopeType
  project_id: string | null
}

export interface ManagedSkillPermissions {
  read: Array<'bound_project_snapshot'>
  write: Array<'run_staging'>
  network: false
  secrets: string[]
}

export interface ManagedSkillManifestSource {
  type: 'builtin_managed'
  reference: string
  license: string | null
  source_url: string | null
  source_version: string | null
  modified: boolean
}

export interface ManagedSkillManifest {
  schema: 'zhimeng.skill-manifest.v1'
  id: string
  display_name: string
  description: string
  kind: ManagedSkillKind
  version: string
  source: ManagedSkillManifestSource
  entrypoint: string | null
  approval: 'none'
  permissions: ManagedSkillPermissions
  timeout_seconds: number | null
  exit_codes: null | { '0': 'success'; '1': 'domain_blocked' }
  artifacts: string[]
  capabilities: string[]
  resources: string[]
  input_schema: string | null
  output_schema: string | null
}

export interface ManagedSkillSourceRecord {
  kind: ManagedSkillSourceKind
  reference: string
  locator_hash: string
  package_fingerprint: string
}

export interface ManagedSkillAdaptationRecord {
  adapter_id: 'zhimeng.managed-package'
  adapter_version: '1'
  adapter_fingerprint: string
  status: 'validated'
}

export interface ManagedSkillValidationRecord {
  status: 'passed'
  contract_status: 'passed' | 'not_applicable'
  isolation_status: 'passed'
  input_contract_fingerprint: string | null
  output_contract_fingerprint: string | null
  contract_bundle_fingerprint: string | null
  permissions: ManagedSkillPermissions
}

export interface ManagedSkillVersionRecord {
  skill_version_id: string
  manifest: ManagedSkillManifest
  manifest_fingerprint: string
  package_fingerprint: string
  file_count: number
  total_bytes: number
  source: ManagedSkillSourceRecord
  adaptation: ManagedSkillAdaptationRecord
  validation: ManagedSkillValidationRecord
  imported_at: string
}

export interface ManagedSkillActivationRecord {
  scope: ManagedSkillScope
  skill_id: string
  configured_state: ManagedSkillConfiguredState
  configured_version_id: string | null
  state_revision: number
  effective_state: ManagedSkillEffectiveState
  effective_version_id: string | null
  effective_scope: ManagedSkillEffectiveScope
  updated_at: string | null
}

export interface ManagedSkillLifecycleCatalogResponse {
  schema: 'zhimeng.managed-skill-lifecycle-catalog.v1'
  scope: ManagedSkillScope
  versions: ManagedSkillVersionRecord[]
  activations: ManagedSkillActivationRecord[]
}

export interface ManagedSkillImportRequest {
  source_path: string
  expected_package_fingerprint?: string | null
}

export interface ManagedSkillImportReceipt {
  receipt_id: string
  scope: ManagedSkillScope
  skill_version_id: string
  source: ManagedSkillSourceRecord
  manifest_fingerprint: string
  file_count: number
  total_bytes: number
  adaptation: ManagedSkillAdaptationRecord
  validation: ManagedSkillValidationRecord
  receipt_hash: string
  created_at: string
}

export interface ManagedSkillImportResponse {
  schema: 'zhimeng.managed-skill-import.v1'
  imported: boolean
  version: ManagedSkillVersionRecord
  activation: ManagedSkillActivationRecord
  receipt: ManagedSkillImportReceipt
}

export interface ManagedSkillActivationRequest {
  action: ManagedSkillActivationAction
  skill_version_id?: string | null
  expected_state_revision: number
  expected_version_id?: string | null
}

export interface ManagedSkillActivationReceipt {
  receipt_id: string
  scope: ManagedSkillScope
  skill_id: string
  action:
    | 'builtin_bootstrap'
    | 'enable'
    | 'disable'
    | 'upgrade'
    | 'rollback'
    | 'inherit'
  previous_state: ManagedSkillConfiguredState
  previous_version_id: string | null
  next_state: Exclude<ManagedSkillConfiguredState, 'unconfigured'>
  next_version_id: string | null
  expected_state_revision: number
  expected_version_id: string | null
  state_revision: number
  receipt_hash: string
  created_at: string
}

export interface ManagedSkillActivationResponse {
  schema: 'zhimeng.managed-skill-activation.v1'
  activation: ManagedSkillActivationRecord
  receipt: ManagedSkillActivationReceipt
}

function applicationPath(suffix = ''): string {
  return `/api/v1/managed-skills${suffix}`
}

function projectPath(projectId: string, suffix = ''): string {
  return `/api/v1/projects/${encodeURIComponent(projectId)}/managed-skills${suffix}`
}

export function listApplicationManagedSkills(options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<ManagedSkillLifecycleCatalogResponse>(applicationPath(), options)
}

export function listProjectManagedSkills(projectId: string, options: RuntimeClientOptions = {}) {
  return fetchRuntimeJson<ManagedSkillLifecycleCatalogResponse>(projectPath(projectId), options)
}

export function importApplicationManagedSkill(
  request: ManagedSkillImportRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillImportResponse>(applicationPath('/imports'), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function importProjectManagedSkill(
  projectId: string,
  request: ManagedSkillImportRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillImportResponse>(projectPath(projectId, '/imports'), options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(request),
  })
}

export function activateApplicationManagedSkill(
  skillId: string,
  request: ManagedSkillActivationRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillActivationResponse>(
    applicationPath(`/${encodeURIComponent(skillId)}/activation`),
    options,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}

export function activateProjectManagedSkill(
  projectId: string,
  skillId: string,
  request: ManagedSkillActivationRequest,
  options: RuntimeClientOptions = {},
) {
  return fetchRuntimeJson<ManagedSkillActivationResponse>(
    projectPath(projectId, `/${encodeURIComponent(skillId)}/activation`),
    options,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(request),
    },
  )
}

import type {
  ManagedSkillActivationRecord,
  ManagedSkillActivationResponse,
  ManagedSkillLifecycleCatalogResponse,
  ManagedSkillManifest,
  ManagedSkillVersionRecord,
} from '../api/managedSkills'

const hash = (seed: string, size = 64): string => seed.padEnd(size, seed).slice(0, size)

export function managedSkillManifestFixture(
  overrides: Partial<ManagedSkillManifest> = {},
): ManagedSkillManifest {
  return {
    schema: 'zhimeng.skill-manifest.v1',
    id: 'novel-creation-suite.project-audit',
    display_name: 'Creation Suite Project Audit',
    description: '只读项目审计，不写正式正文。',
    kind: 'deterministic_tool',
    version: '2026.07.22',
    source: {
      type: 'builtin_managed',
      reference: 'creation-suite-project-audit',
      license: null,
      source_url: null,
      source_version: null,
      modified: false,
    },
    entrypoint: 'run_project_audit.py',
    approval: 'none',
    permissions: {
      read: ['bound_project_snapshot'],
      write: ['run_staging'],
      network: false,
      secrets: [],
    },
    timeout_seconds: 60,
    exit_codes: { '0': 'success', '1': 'domain_blocked' },
    artifacts: ['project-audit.json'],
    capabilities: ['project-audit'],
    resources: [],
    input_schema: 'https://zhimeng.local/contracts/project-audit-input.schema.json',
    output_schema: 'https://zhimeng.local/contracts/project-audit-output.schema.json',
    ...overrides,
  }
}

export function managedSkillVersionFixture(
  overrides: Partial<ManagedSkillVersionRecord> = {},
): ManagedSkillVersionRecord {
  const { manifest: manifestOverride, ...rest } = overrides
  const manifest = managedSkillManifestFixture(manifestOverride)
  return {
    skill_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    manifest_fingerprint: hash('1'),
    package_fingerprint: hash('2'),
    file_count: 4,
    total_bytes: 2048,
    source: {
      kind: 'builtin_bundle',
      reference: 'creation-suite-project-audit',
      locator_hash: hash('3'),
      package_fingerprint: hash('2'),
    },
    adaptation: {
      adapter_id: 'zhimeng.managed-package',
      adapter_version: '1',
      adapter_fingerprint: hash('4'),
      status: 'validated',
    },
    validation: {
      status: 'passed',
      contract_status: 'passed',
      isolation_status: 'passed',
      input_contract_fingerprint: hash('5'),
      output_contract_fingerprint: hash('6'),
      contract_bundle_fingerprint: hash('7'),
      permissions: {
        read: ['bound_project_snapshot'],
        write: ['run_staging'],
        network: false,
        secrets: [],
      },
    },
    imported_at: '2026-07-22T08:00:00Z',
    ...rest,
    manifest,
  }
}

export function managedSkillActivationFixture(
  overrides: Partial<ManagedSkillActivationRecord> = {},
): ManagedSkillActivationRecord {
  return {
    scope: {
      scope_type: 'project',
      project_id: 'project-lifecycle',
    },
    skill_id: 'novel-creation-suite.project-audit',
    configured_state: 'inherited',
    configured_version_id: null,
    state_revision: 0,
    effective_state: 'enabled',
    effective_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    effective_scope: 'application',
    updated_at: '2026-07-22T08:00:00Z',
    ...overrides,
  }
}

export function managedSkillLifecycleCatalogFixture(
  overrides: Partial<ManagedSkillLifecycleCatalogResponse> = {},
): ManagedSkillLifecycleCatalogResponse {
  const version = managedSkillVersionFixture()
  return {
    schema: 'zhimeng.managed-skill-lifecycle-catalog.v1',
    scope: {
      scope_type: 'project',
      project_id: 'project-lifecycle',
    },
    versions: [version],
    activations: [managedSkillActivationFixture()],
    ...overrides,
  }
}

export function managedSkillActivationResponseFixture(
  overrides: Partial<ManagedSkillActivationResponse> = {},
): ManagedSkillActivationResponse {
  const activation = managedSkillActivationFixture({
    configured_state: 'enabled',
    configured_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    state_revision: 1,
    effective_state: 'enabled',
    effective_version_id: 'skill-version-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',
    effective_scope: 'project',
  })
  return {
    schema: 'zhimeng.managed-skill-activation.v1',
    activation,
    receipt: {
      receipt_id: 'skill-activation-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',
      scope: activation.scope,
      skill_id: activation.skill_id,
      action: 'enable',
      previous_state: 'inherited',
      previous_version_id: null,
      next_state: 'enabled',
      next_version_id: activation.configured_version_id,
      expected_state_revision: 0,
      expected_version_id: null,
      state_revision: 1,
      receipt_hash: hash('8'),
      created_at: '2026-07-22T08:01:00Z',
    },
    ...overrides,
  }
}

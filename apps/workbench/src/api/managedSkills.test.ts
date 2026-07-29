import { describe, expect, it, vi } from 'vitest'
import {
  activateApplicationManagedSkill,
  activateProjectManagedSkill,
  importApplicationManagedSkill,
  importProjectManagedSkill,
  listApplicationManagedSkills,
  listProjectManagedSkills,
} from './managedSkills'
import {
  managedSkillActivationResponseFixture,
  managedSkillLifecycleCatalogFixture,
} from '../test/managedSkillFixture'

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  })
}

describe('managed skill lifecycle API client', () => {
  it('uses application and project lifecycle routes with CAS bodies', async () => {
    const catalog = managedSkillLifecycleCatalogFixture()
    const activation = managedSkillActivationResponseFixture()
    const fetcher = vi
      .fn<typeof fetch>()
      .mockResolvedValueOnce(jsonResponse(catalog))
      .mockResolvedValueOnce(jsonResponse(catalog))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.managed-skill-import.v1',
        imported: true,
        version: catalog.versions[0],
        activation: catalog.activations[0],
        receipt: {
          receipt_id: `skill-import-${'c'.repeat(32)}`,
          scope: catalog.scope,
          skill_version_id: catalog.versions[0].skill_version_id,
          source: catalog.versions[0].source,
          manifest_fingerprint: catalog.versions[0].manifest_fingerprint,
          file_count: catalog.versions[0].file_count,
          total_bytes: catalog.versions[0].total_bytes,
          adaptation: catalog.versions[0].adaptation,
          validation: catalog.versions[0].validation,
          receipt_hash: '9'.repeat(64),
          created_at: '2026-07-22T08:02:00Z',
        },
      }))
      .mockResolvedValueOnce(jsonResponse({
        schema: 'zhimeng.managed-skill-import.v1',
        imported: true,
        version: catalog.versions[0],
        activation: catalog.activations[0],
        receipt: {
          receipt_id: `skill-import-${'d'.repeat(32)}`,
          scope: catalog.scope,
          skill_version_id: catalog.versions[0].skill_version_id,
          source: catalog.versions[0].source,
          manifest_fingerprint: catalog.versions[0].manifest_fingerprint,
          file_count: catalog.versions[0].file_count,
          total_bytes: catalog.versions[0].total_bytes,
          adaptation: catalog.versions[0].adaptation,
          validation: catalog.versions[0].validation,
          receipt_hash: 'a'.repeat(64),
          created_at: '2026-07-22T08:03:00Z',
        },
      }))
      .mockResolvedValueOnce(jsonResponse(activation))
      .mockResolvedValueOnce(jsonResponse(activation))

    const options = { baseUrl: 'http://127.0.0.1:8787', fetcher }
    const projectId = 'project/a b?'
    const skillId = 'novel-creation-suite.project-audit'

    await listApplicationManagedSkills(options)
    await listProjectManagedSkills(projectId, options)
    await importApplicationManagedSkill({ source_path: 'C:/skills/package' }, options)
    await importProjectManagedSkill(projectId, {
      source_path: 'C:/skills/package',
      expected_package_fingerprint: '2'.repeat(64),
    }, options)
    await activateApplicationManagedSkill(skillId, {
      action: 'enable',
      skill_version_id: catalog.versions[0].skill_version_id,
      expected_state_revision: 0,
      expected_version_id: null,
    }, options)
    await activateProjectManagedSkill(projectId, skillId, {
      action: 'disable',
      skill_version_id: null,
      expected_state_revision: 1,
      expected_version_id: catalog.versions[0].skill_version_id,
    }, options)

    expect(fetcher.mock.calls.map(([url, init]) => ({
      url,
      method: init?.method ?? 'GET',
      body: init?.body === undefined ? undefined : JSON.parse(String(init.body)),
    }))).toEqual([
      {
        url: 'http://127.0.0.1:8787/api/v1/managed-skills',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/managed-skills',
        method: 'GET',
        body: undefined,
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/managed-skills/imports',
        method: 'POST',
        body: { source_path: 'C:/skills/package' },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/managed-skills/imports',
        method: 'POST',
        body: {
          source_path: 'C:/skills/package',
          expected_package_fingerprint: '2'.repeat(64),
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/managed-skills/novel-creation-suite.project-audit/activation',
        method: 'PUT',
        body: {
          action: 'enable',
          skill_version_id: catalog.versions[0].skill_version_id,
          expected_state_revision: 0,
          expected_version_id: null,
        },
      },
      {
        url: 'http://127.0.0.1:8787/api/v1/projects/project%2Fa%20b%3F/managed-skills/novel-creation-suite.project-audit/activation',
        method: 'PUT',
        body: {
          action: 'disable',
          skill_version_id: null,
          expected_state_revision: 1,
          expected_version_id: catalog.versions[0].skill_version_id,
        },
      },
    ])
  })
})

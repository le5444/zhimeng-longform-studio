import { describe, expect, it, vi } from 'vitest'
import {
  materializeSkillAdaptation,
  planSkillAdaptation,
} from './skillAdaptations'

function jsonResponse(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { 'Content-Type': 'application/json' },
  })
}

const planFixture = {
  schema: 'zhimeng.skill-adaptation-plan-response.v1',
  plan: {
    schema: 'zhimeng.skill-adaptation-plan.v1',
    plan_id: 'skill-adaptation-plan-' + 'a'.repeat(32),
    adapter_id: 'zhimeng.raw-skill-instruction',
    adapter_version: '1',
    adapter_fingerprint: 'b'.repeat(64),
    status: 'ready_to_materialize',
    source: {
      source_path: 'C:/skills/sample',
      skill_md_path: 'C:/skills/sample/SKILL.md',
      skill_md_hash: 'c'.repeat(64),
      skill_md_bytes: 120,
      frontmatter_name: 'sample-volume-folder',
      frontmatter_description: 'Raw instruction only.',
      locator_hash: 'd'.repeat(64),
    },
    target: {
      skill_id: 'adapted.sample-volume-folder',
      display_name: 'sample-volume-folder',
      description: 'Raw instruction only.',
      kind: 'instruction_source',
      version: '2026.07.26-adapted.1',
      capability: 'raw-skill.instruction-source.v1',
      resources: ['instruction-map.json', 'resources/SKILL.md'],
      formal_draft_allowed: false,
      executable: false,
    },
    missing: [
      {
        code: 'managed_manifest_missing',
        message: 'Raw skill has no skill.manifest.json; adapter will synthesize an inert package.',
      },
    ],
    not_claiming: [
      'four_skills_generic_exactly_once',
      'raw_skill_md_execution',
      'deterministic_tool_auto_generation',
      'formal_prose_writeback',
      'local_v1_exe',
    ],
    created_at: '2026-07-26T00:00:00+00:00',
    plan_hash: 'e'.repeat(64),
  },
}

const materializeFixture = {
  schema: 'zhimeng.skill-adaptation-materialization-response.v1',
  materialization: {
    schema: 'zhimeng.skill-adaptation-materialization.v1',
    materialization_id: 'skill-adaptation-materialization-' + 'f'.repeat(32),
    plan_id: planFixture.plan.plan_id,
    plan_hash: planFixture.plan.plan_hash,
    package_root: 'C:/runtime/managed-skill-adaptations/adapted.sample-volume-folder',
    package_fingerprint: '1'.repeat(64),
    skill_id: 'adapted.sample-volume-folder',
    skill_version: '2026.07.26-adapted.1',
    kind: 'instruction_source',
    formal_draft_allowed: false,
    imported: true,
    skill_version_id: 'skill-version-' + '2'.repeat(32),
    import_receipt_id: 'skill-import-' + '3'.repeat(32),
    not_claiming: planFixture.plan.not_claiming,
    created_at: '2026-07-26T00:01:00+00:00',
    materialization_hash: '4'.repeat(64),
  },
  import_result: { imported: true },
}

describe('skillAdaptations API', () => {
  it('plans a raw skill adaptation', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(planFixture))
    const result = await planSkillAdaptation(
      { source_path: 'C:/skills/sample' },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.plan.target.kind).toBe('instruction_source')
    expect(result.plan.target.formal_draft_allowed).toBe(false)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/managed-skills/adaptations/plan',
    )
    expect(JSON.parse(String(fetcher.mock.calls[0]?.[1]?.body))).toEqual({
      source_path: 'C:/skills/sample',
    })
  })

  it('materializes and imports an inert package', async () => {
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(jsonResponse(materializeFixture))
    const result = await materializeSkillAdaptation(
      {
        expected_plan_id: planFixture.plan.plan_id,
        expected_plan_hash: planFixture.plan.plan_hash,
        import_after_materialize: true,
      },
      { fetcher, baseUrl: 'http://127.0.0.1:8787' },
    )
    expect(result.materialization.imported).toBe(true)
    expect(result.materialization.formal_draft_allowed).toBe(false)
    expect(fetcher.mock.calls[0]?.[0]).toBe(
      'http://127.0.0.1:8787/api/v1/managed-skills/adaptations/materialize',
    )
  })
})

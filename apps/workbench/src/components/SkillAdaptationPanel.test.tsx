import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { SkillAdaptationPanel } from './SkillAdaptationPanel'

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
        message: 'Raw skill has no skill.manifest.json',
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

describe('SkillAdaptationPanel', () => {
  const originalFetch = globalThis.fetch

  beforeEach(() => {
    globalThis.fetch = vi.fn()
  })

  afterEach(() => {
    globalThis.fetch = originalFetch
  })

  it('plans and materializes a raw skill into an inert package', async () => {
    const user = userEvent.setup()
    const onImported = vi.fn()
    vi.mocked(globalThis.fetch)
      .mockResolvedValueOnce(new Response(JSON.stringify(planFixture), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))
      .mockResolvedValueOnce(new Response(JSON.stringify(materializeFixture), {
        status: 200,
        headers: { 'Content-Type': 'application/json' },
      }))

    render(<SkillAdaptationPanel enabled onImported={onImported} />)

    await user.type(screen.getByLabelText('受信任 raw skill 路径'), 'C:\\skills\\sample')
    await user.click(screen.getByRole('button', { name: '生成适配计划' }))

    await waitFor(() => {
      expect(screen.getByText('适配计划已就绪')).toBeInTheDocument()
    })
    expect(screen.getByText(/adapted\.sample-volume-folder/)).toBeInTheDocument()
    expect(screen.getByText(/managed_manifest_missing/)).toBeInTheDocument()

    await user.click(screen.getByRole('button', { name: '物化并导入' }))
    await waitFor(() => {
      expect(screen.getByText('已物化并 inert 导入')).toBeInTheDocument()
    })
    expect(onImported).toHaveBeenCalled()
    expect(String(vi.mocked(globalThis.fetch).mock.calls[0]?.[0])).toContain('/adaptations/plan')
    expect(String(vi.mocked(globalThis.fetch).mock.calls[1]?.[0])).toContain('/adaptations/materialize')
  })
})

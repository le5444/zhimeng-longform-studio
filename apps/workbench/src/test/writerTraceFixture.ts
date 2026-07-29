import type {
  AgentCandidateOutputV3,
  AgentWorkflowState,
  CreationInstructionActivationBinding,
  CreationInstructionBlock,
  CreationInstructionDispatchReceipt,
  CreationInstructionMaterialization,
  WriterCandidateResult,
  WriterMode,
  WriterTargetFile,
  WriterTask,
  WriterTraceResponse,
} from '../api/agent'
import { canonicalJsonHash } from '../lib/canonicalJson'
import { agentCandidateOutputFixture } from './agentCandidateOutputFixture'
import { agentWorkflowStateFixture } from './agentWorkflowFixture'

export interface WriterTraceFixture {
  workflow: AgentWorkflowState
  task: WriterTask
  activation: CreationInstructionActivationBinding
  materialization: CreationInstructionMaterialization
  dispatch: CreationInstructionDispatchReceipt
  candidate: AgentCandidateOutputV3
  result: WriterCandidateResult
  response: WriterTraceResponse
}

async function utf8Hash(value: string): Promise<string> {
  const digest = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(value))
  return [...new Uint8Array(digest)]
    .map((byte) => byte.toString(16).padStart(2, '0'))
    .join('')
}

export async function writerTraceFixture(
  mode: WriterMode = 'repair_only',
): Promise<WriterTraceFixture> {
  const baseCandidate = await agentCandidateOutputFixture()
  if (baseCandidate.schema !== 'zhimeng.agent-candidate-output.v2') {
    throw new Error('Writer Trace fixture requires the legacy v2 base fixture')
  }
  const targetFiles: WriterTargetFile[] = mode === 'author_review_change_proposal'
    ? [{
        file_id: 'file-0123456789abcdef0123456789',
        relative_path: '05_正文内容/第一卷/CH001.md',
        content_hash: '7'.repeat(64),
        content_role: 'prose_body',
      }]
    : []
  const targetFilesHash = await canonicalJsonHash(targetFiles)
  const creationHandoffHash = '4'.repeat(64)
  const taskId = `writer-task-${(await canonicalJsonHash({
    schema: 'zhimeng.writer-task.v1',
    run_id: baseCandidate.run_id,
    final_checkpoint_id: baseCandidate.final_checkpoint_id,
    creation_handoff_hash: creationHandoffHash,
    mode,
    target_files_hash: targetFilesHash,
  })).slice(0, 32)}`
  const taskMaterial = {
    schema: 'zhimeng.writer-task.v1' as const,
    writer_task_id: taskId,
    mode,
    run_id: baseCandidate.run_id,
    workflow_plan_id: baseCandidate.workflow_plan_id,
    workflow_plan_hash: baseCandidate.workflow_plan_hash,
    final_checkpoint_id: baseCandidate.final_checkpoint_id,
    final_checkpoint_hash: baseCandidate.final_checkpoint_hash,
    creation_handoff_hash: creationHandoffHash,
    creation_handoff_status: baseCandidate.handoff_status,
    context_pack_id: baseCandidate.context_pack_id,
    context_pack_hash: baseCandidate.context_pack_hash,
    route_receipt_id: `nlcl-route-${'5'.repeat(32)}`,
    route_receipt_hash: '5'.repeat(64),
    operation_package_id: baseCandidate.operation_package_id,
    operation_package_hash: baseCandidate.operation_package_hash,
    project_id: baseCandidate.project_id,
    project_revision: baseCandidate.project_revision,
    project_fingerprint: baseCandidate.project_fingerprint,
    active_chapter_id: 'chapter-001',
    task_goal: mode === 'author_review_change_proposal'
      ? '使用正式知识为当前章节生成供作者审阅的正文修改候选。'
      : '修复写前门禁问题。',
    failure_signatures: baseCandidate.failure_signatures,
    target_files: targetFiles,
    target_files_hash: targetFilesHash,
    knowledge_fingerprint: '6'.repeat(64),
    selected_card_ids: ['work-stage-pressure-loop'],
    selected_card_hashes: ['6'.repeat(64)],
    formal_draft_allowed: false as const,
    writes_project_truth: false as const,
    requires_author_review: true as const,
    created_at: '2026-07-22T01:00:01Z',
  }
  const task: WriterTask = {
    ...taskMaterial,
    task_hash: await canonicalJsonHash(taskMaterial),
  }

  const skillVersionId = `skill-version-${'2'.repeat(32)}`
  const scopeChain = [{
    scope_type: 'project' as const,
    scope_key: `project:${task.project_id}`,
    configured_state: 'enabled' as const,
    state_revision: 1,
    activation_receipt_id: `skill-activation-${'3'.repeat(32)}`,
    activation_receipt_hash: '3'.repeat(64),
    configured_version_id: skillVersionId,
  }]
  const activationId = `creation-instruction-activation-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-activation.v1',
    run_id: task.run_id,
    workflow_plan_id: task.workflow_plan_id,
    workflow_plan_hash: task.workflow_plan_hash,
    scope_chain: scopeChain,
    skill_version_id: skillVersionId,
    package_fingerprint: '4'.repeat(64),
    instruction_map_hash: '5'.repeat(64),
  })).slice(0, 32)}`
  const activationMaterial = {
    schema: 'zhimeng.creation-instruction-activation.v1' as const,
    activation_binding_id: activationId,
    run_id: task.run_id,
    project_id: task.project_id,
    workflow_plan_id: task.workflow_plan_id,
    workflow_plan_hash: task.workflow_plan_hash,
    skill_id: 'novel-creation-suite.writer-instruction' as const,
    scope_chain: scopeChain,
    effective_scope: 'project' as const,
    skill_version_id: skillVersionId,
    skill_version: '1.0.0',
    manifest_fingerprint: '3'.repeat(64),
    package_fingerprint: '4'.repeat(64),
    instruction_map_path: 'instruction-map.json' as const,
    instruction_map_hash: '5'.repeat(64),
    created_at: '2026-07-22T01:00:01Z',
  }
  const activation: CreationInstructionActivationBinding = {
    ...activationMaterial,
    binding_hash: await canonicalJsonHash(activationMaterial),
  }

  const instruction = 'Follow the Writer Task. Return a review-only candidate.\n'
  const instructionBytes = new TextEncoder().encode(instruction).length
  const blocks: CreationInstructionBlock[] = [{
    block_id: 'writer-core',
    title: 'Writer core',
    source_path: 'SKILL.md',
    source_line_start: 1,
    source_line_end: 2,
    source_byte_start: 0,
    source_byte_end: instructionBytes,
    utf8_bytes: instructionBytes,
    content_hash: await utf8Hash(instruction),
    included: true,
    content: instruction,
    exclusion_reason: null,
  }, {
    block_id: 'epub-export',
    title: 'EPUB export',
    source_path: 'SKILL.md',
    source_line_start: 3,
    source_line_end: 4,
    source_byte_start: instructionBytes,
    source_byte_end: instructionBytes + 12,
    utf8_bytes: 12,
    content_hash: '8'.repeat(64),
    included: false,
    content: null,
    exclusion_reason: 'Runtime writer dispatch does not export EPUB.',
  }]
  const blocksHash = await canonicalJsonHash(blocks)
  const providerInstructionHash = await utf8Hash(instruction)
  const materializationId = `creation-instruction-materialization-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-materialization.v1',
    run_id: task.run_id,
    writer_task_id: task.writer_task_id,
    activation_binding_id: activation.activation_binding_id,
    blocks_hash: blocksHash,
    provider_instruction_hash: providerInstructionHash,
  })).slice(0, 32)}`
  const materializationMaterial = {
    schema: 'zhimeng.creation-instruction-materialization.v1' as const,
    materialization_id: materializationId,
    run_id: task.run_id,
    project_id: task.project_id,
    project_revision: task.project_revision,
    project_fingerprint: task.project_fingerprint,
    workflow_plan_id: task.workflow_plan_id,
    workflow_plan_hash: task.workflow_plan_hash,
    final_checkpoint_id: task.final_checkpoint_id,
    final_checkpoint_hash: task.final_checkpoint_hash,
    writer_task_id: task.writer_task_id,
    writer_task_hash: task.task_hash,
    activation_binding_id: activation.activation_binding_id,
    activation_binding_hash: activation.binding_hash,
    skill_version_id: activation.skill_version_id,
    manifest_fingerprint: activation.manifest_fingerprint,
    package_fingerprint: activation.package_fingerprint,
    instruction_map_path: 'instruction-map.json' as const,
    instruction_map_hash: activation.instruction_map_hash,
    source_path: 'SKILL.md',
    source_hash: '6'.repeat(64),
    blocks,
    blocks_hash: blocksHash,
    included_block_ids: ['writer-core'],
    provider_instruction: instruction,
    provider_instruction_hash: providerInstructionHash,
    provider_instruction_utf8_bytes: instructionBytes,
    max_included_utf8_bytes: 200_000,
    formal_draft_allowed: false as const,
    writes_project_truth: false as const,
    requires_author_review: true as const,
    created_at: '2026-07-22T01:00:02Z',
  }
  const materialization: CreationInstructionMaterialization = {
    ...materializationMaterial,
    materialization_hash: await canonicalJsonHash(materializationMaterial),
  }

  const baseWorkflow = agentWorkflowStateFixture({
    runId: task.run_id,
    projectId: task.project_id,
    runStatus: 'succeeded',
    providerBoundary: true,
  })
  const checkpoints = baseWorkflow.checkpoints.map((checkpoint, index) => (
    index === baseWorkflow.checkpoints.length - 1
      ? {
          ...checkpoint,
          checkpoint_id: task.final_checkpoint_id,
          checkpoint_hash: task.final_checkpoint_hash,
        }
      : checkpoint
  ))
  const boundary = {
    ...baseWorkflow.provider_boundary!,
    boundary_id: baseCandidate.provider_boundary_id,
    boundary_hash: baseCandidate.provider_boundary_hash,
    request_hash: baseCandidate.provider_request_hash,
    workflow_plan_id: task.workflow_plan_id,
    plan_hash: task.workflow_plan_hash,
    last_step_checkpoint_id: task.final_checkpoint_id,
    last_step_checkpoint_hash: task.final_checkpoint_hash,
  }
  const workflow: AgentWorkflowState = {
    ...baseWorkflow,
    plan: {
      ...baseWorkflow.plan,
      workflow_plan_id: task.workflow_plan_id,
      plan_hash: task.workflow_plan_hash,
      project_revision: task.project_revision,
      project_fingerprint: task.project_fingerprint,
      context_pack_id: task.context_pack_id,
      context_pack_hash: task.context_pack_hash,
      route_receipt_id: task.route_receipt_id,
      route_receipt_hash: task.route_receipt_hash,
      operation_package_id: task.operation_package_id,
      operation_package_hash: task.operation_package_hash,
    },
    checkpoints,
    provider_boundary: boundary,
    last_checkpoint_id: task.final_checkpoint_id,
    last_checkpoint_hash: task.final_checkpoint_hash,
    resume_state: 'terminal',
    resume_allowed: false,
    reason_code: 'run_terminal',
  }

  const dispatchId = `creation-instruction-dispatch-${(await canonicalJsonHash({
    schema: 'zhimeng.creation-instruction-dispatch-receipt.v1',
    run_id: task.run_id,
    provider_boundary_id: boundary.boundary_id,
    materialization_id: materialization.materialization_id,
  })).slice(0, 32)}`
  const dispatchMaterial = {
    schema: 'zhimeng.creation-instruction-dispatch-receipt.v1' as const,
    dispatch_receipt_id: dispatchId,
    run_id: task.run_id,
    workflow_plan_id: task.workflow_plan_id,
    workflow_plan_hash: task.workflow_plan_hash,
    final_checkpoint_id: task.final_checkpoint_id,
    final_checkpoint_hash: task.final_checkpoint_hash,
    writer_task_id: task.writer_task_id,
    writer_task_hash: task.task_hash,
    materialization_id: materialization.materialization_id,
    materialization_hash: materialization.materialization_hash,
    provider_boundary_id: boundary.boundary_id,
    provider_boundary_hash: boundary.boundary_hash,
    provider_request_hash: boundary.request_hash,
    provider_messages_hash: '7'.repeat(64),
    tool_definitions_hash: '8'.repeat(64),
    provider_dispatch_config_fingerprint: boundary.provider_dispatch_config_fingerprint,
    automatic_provider_replay_count: 0 as const,
    status: 'committed_before_dispatch' as const,
    formal_draft_allowed: false as const,
    writes_project_truth: false as const,
    created_at: '2026-07-22T01:00:03Z',
  }
  const dispatch: CreationInstructionDispatchReceipt = {
    ...dispatchMaterial,
    receipt_hash: await canonicalJsonHash(dispatchMaterial),
  }

  const changeSetId = mode === 'author_review_change_proposal'
    ? `changeset-${'8'.repeat(32)}`
    : null
  const changeSetHash = mode === 'author_review_change_proposal' ? '8'.repeat(64) : null
  const content = mode === 'author_review_change_proposal'
    ? {
        title: '第一章正文修改候选',
        summary: '已按正式知识生成一项待作者审阅的正文改动。',
        body: '## 改动说明\n\n正文只存在于受审 ChangeSet 中，尚未写回项目。',
        format: 'markdown' as const,
      }
    : baseCandidate.content
  const references = [{
    reference_kind: 'workflow_checkpoint' as const,
    reference_id: task.final_checkpoint_id,
    reference_hash: task.final_checkpoint_hash,
  }, {
    reference_kind: 'runtime_identity' as const,
    reference_id: task.writer_task_id,
    reference_hash: task.task_hash,
  }, {
    reference_kind: 'artifact' as const,
    reference_id: materialization.materialization_id,
    reference_hash: materialization.materialization_hash,
  }, ...(changeSetId && changeSetHash
    ? [{
        reference_kind: 'change_set' as const,
        reference_id: changeSetId,
        reference_hash: changeSetHash,
      }]
    : [])]
  const {
    envelope_hash: _baseEnvelopeHash,
    candidate_output_id: _baseCandidateOutputId,
    schema: _baseSchema,
    references: _baseReferences,
    references_hash: _baseReferencesHash,
    ...baseMaterial
  } = baseCandidate
  const candidateId = `agent-candidate-${(await canonicalJsonHash({
    schema: 'zhimeng.agent-candidate-output.v3',
    run_id: task.run_id,
    provider_boundary_id: boundary.boundary_id,
  })).slice(0, 32)}`
  const candidateMaterial = {
    ...baseMaterial,
    schema: 'zhimeng.agent-candidate-output.v3' as const,
    candidate_output_id: candidateId,
    kind: mode === 'author_review_change_proposal' ? 'change_proposal' as const : 'repair_plan' as const,
    content,
    content_hash: await canonicalJsonHash(content),
    references,
    references_hash: await canonicalJsonHash(references),
    writer_task_id: task.writer_task_id,
    writer_task_hash: task.task_hash,
    instruction_materialization_id: materialization.materialization_id,
    instruction_materialization_hash: materialization.materialization_hash,
    instruction_dispatch_receipt_id: dispatch.dispatch_receipt_id,
    instruction_dispatch_receipt_hash: dispatch.receipt_hash,
  }
  const candidate: AgentCandidateOutputV3 = {
    ...candidateMaterial,
    envelope_hash: await canonicalJsonHash(candidateMaterial),
  }
  const resultId = `writer-candidate-result-${(await canonicalJsonHash({
    schema: 'zhimeng.writer-candidate-result.v1',
    writer_task_id: task.writer_task_id,
    candidate_output_id: candidate.candidate_output_id,
    change_set_id: changeSetId,
  })).slice(0, 32)}`
  const resultMaterial = {
    schema: 'zhimeng.writer-candidate-result.v1' as const,
    writer_candidate_result_id: resultId,
    mode,
    run_id: task.run_id,
    project_id: task.project_id,
    project_revision: task.project_revision,
    project_fingerprint: task.project_fingerprint,
    writer_task_id: task.writer_task_id,
    writer_task_hash: task.task_hash,
    candidate_output_id: candidate.candidate_output_id,
    candidate_output_hash: candidate.envelope_hash,
    candidate_kind: mode === 'author_review_change_proposal' ? 'change_proposal' as const : 'repair_plan' as const,
    change_set_id: changeSetId,
    change_set_hash: changeSetHash,
    target_files: task.target_files,
    target_files_hash: task.target_files_hash,
    formal_draft_allowed: false as const,
    writes_project_truth: false as const,
    requires_author_review: true as const,
    created_at: '2026-07-22T01:00:04Z',
  }
  const result: WriterCandidateResult = {
    ...resultMaterial,
    result_hash: await canonicalJsonHash(resultMaterial),
  }
  const response: WriterTraceResponse = {
    schema: 'zhimeng.writer-trace.v1',
    run_id: task.run_id,
    workflow,
    writer_task: task,
    activation_binding: activation,
    materialization,
    dispatch_receipt: dispatch,
    candidate_output: candidate,
    writer_candidate_result: result,
    provider_recovery: null,
  }
  return {
    workflow,
    task,
    activation,
    materialization,
    dispatch,
    candidate,
    result,
    response,
  }
}

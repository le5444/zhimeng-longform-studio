import type {
  AgentProviderIoBoundary,
  AgentRunStatus,
  AgentWorkflowState,
  AgentWorkflowStep,
  AgentWorkflowStepCheckpoint,
} from '../api/agent'

interface AgentWorkflowFixtureOptions {
  runId?: string
  projectId?: string
  runStatus?: AgentRunStatus
  providerBoundary?: boolean
  completedSteps?: 0 | 1 | 2 | 3
}

const hash = (digit: string) => digit.repeat(64)

function step(
  ordinal: number,
  skillId: string,
  contractName: string,
  inputBuilderName: string,
): AgentWorkflowStep {
  return {
    step_id: `workflow-step-${String(ordinal).repeat(32)}`,
    ordinal,
    runner: 'managed_skill',
    skill_id: skillId,
    expected_skill_version_id: `skill-version-${String(ordinal + 2).repeat(32)}`,
    expected_skill_version: '1',
    expected_manifest_fingerprint: hash(String(ordinal + 2)),
    expected_package_fingerprint: hash(String(ordinal + 4)),
    input_contract: `https://zhimeng.local/contracts/${contractName}-request.schema.json`,
    output_contract: `https://zhimeng.local/contracts/${contractName}-result.schema.json`,
    input_builder_id: `zhimeng.workflow-input.${inputBuilderName}`,
    input_builder_version: '1',
    max_attempts: 1,
    continue_on_domain_status: ['healthy', 'degraded', 'blocked'],
    formal_draft_allowed: false,
    step_hash: hash(String(ordinal + 5)),
  }
}

function checkpoint(
  runId: string,
  workflowPlanId: string,
  planHash: string,
  workflowStep: AgentWorkflowStep,
  previous: AgentWorkflowStepCheckpoint | null,
): AgentWorkflowStepCheckpoint {
  const ordinal = workflowStep.ordinal
  return {
    schema: 'zhimeng.workflow-step-checkpoint.v1',
    checkpoint_id: `workflow-checkpoint-${String(ordinal).repeat(32)}`,
    workflow_plan_id: workflowPlanId,
    plan_hash: planHash,
    run_id: runId,
    step_id: workflowStep.step_id,
    step_hash: workflowStep.step_hash,
    step_ordinal: ordinal,
    runner: 'managed_skill',
    skill_id: workflowStep.skill_id,
    expected_skill_version_id: workflowStep.expected_skill_version_id,
    expected_skill_version: workflowStep.expected_skill_version,
    expected_manifest_fingerprint: workflowStep.expected_manifest_fingerprint,
    expected_package_fingerprint: workflowStep.expected_package_fingerprint,
    attempt_ordinal: 1,
    previous_checkpoint_id: previous?.checkpoint_id ?? null,
    previous_checkpoint_hash: previous?.checkpoint_hash ?? null,
    tool_call_id: `tool-workflow-${ordinal}`,
    provider_tool_call_id: `provider-workflow-${ordinal}`,
    skill_call_id: `skill-call-workflow-${ordinal}`,
    skill_receipt_id: `skill-receipt-${String(ordinal).repeat(32)}`,
    skill_receipt_hash: hash(String(ordinal + 1)),
    route_execution_receipt_id: `nlcl-route-exec-${String(ordinal).repeat(32)}`,
    route_execution_receipt_hash: hash(String(ordinal + 2)),
    input_hash: hash(String(ordinal + 3)),
    output_hash: hash(String(ordinal + 4)),
    tool_result_hash: hash(String(ordinal + 5)),
    execution_status: 'succeeded',
    domain_status: 'blocked',
    error_code: null,
    error_message: null,
    formal_draft_allowed: false,
    created_at: `2026-07-21T10:00:0${ordinal}Z`,
    checkpoint_hash: hash(String(ordinal + 6)),
  }
}

export function agentWorkflowStateFixture({
  runId = 'run-0123456789abcdef0123456789',
  projectId = 'project-0123456789abcdef01234567',
  runStatus = 'paused',
  providerBoundary = false,
  completedSteps = 1,
}: AgentWorkflowFixtureOptions = {}): AgentWorkflowState {
  const steps = [
    step(
      1,
      'novel-creation-suite.project-audit',
      'creation-project-audit',
      'project-audit',
    ),
    step(
      2,
      'novel-creation-suite.prewrite-gate',
      'creation-prewrite-gate',
      'prewrite-gate',
    ),
    step(
      3,
      'novel-creation-suite.creation-handoff',
      'creation-handoff',
      'creation-handoff',
    ),
  ]
  const workflowPlanId = `workflow-plan-${'a'.repeat(32)}`
  const planHash = hash('a')
  const completedStepCount = providerBoundary ? steps.length : completedSteps
  const checkpoints: AgentWorkflowStepCheckpoint[] = []
  for (const workflowStep of steps.slice(0, completedStepCount)) {
    checkpoints.push(checkpoint(
      runId,
      workflowPlanId,
      planHash,
      workflowStep,
      checkpoints.at(-1) ?? null,
    ))
  }
  const lastCheckpoint = checkpoints.at(-1) ?? null
  const providerDispatchConfigFingerprint = hash('9')
  const boundary: AgentProviderIoBoundary | null = providerBoundary && lastCheckpoint
    ? {
        schema: 'zhimeng.provider-io-boundary.v1',
        boundary_id: `provider-io-${'b'.repeat(32)}`,
        workflow_plan_id: workflowPlanId,
        plan_hash: planHash,
        run_id: runId,
        request_hash: hash('b'),
        provider_model: 'fixture-model',
        provider_dispatch_config_fingerprint: providerDispatchConfigFingerprint,
        executor: {
          executor_id: 'executor.fixture-workflow',
          kind: 'embedded',
          implementation: 'fixture',
          version: '1',
          protocol_version: '1',
          instance_id: `provider-config-${providerDispatchConfigFingerprint}`,
        },
        last_step_checkpoint_id: lastCheckpoint.checkpoint_id,
        last_step_checkpoint_hash: lastCheckpoint.checkpoint_hash,
        status: 'dispatching',
        formal_draft_allowed: false,
        created_at: '2026-07-21T10:00:04Z',
        boundary_hash: hash('c'),
      }
    : null

  return {
    schema: 'zhimeng.agent-workflow-state.v1',
    run_id: runId,
    run_status: runStatus,
    plan: {
      schema: 'zhimeng.workflow-plan.v1',
      workflow_plan_id: workflowPlanId,
      run_id: runId,
      project_id: projectId,
      project_revision: 1,
      project_fingerprint: hash('d'),
      context_pack_id: 'context-pack-workflow-fixture',
      context_pack_hash: hash('e'),
      knowledge_fingerprint: null,
      route_receipt_id: `nlcl-route-${'f'.repeat(32)}`,
      route_receipt_hash: hash('f'),
      operation_package_id: `prewrite-package-${'1'.repeat(32)}`,
      operation_package_hash: hash('1'),
      steps,
      formal_draft_allowed: false,
      created_at: '2026-07-21T10:00:00Z',
      plan_hash: planHash,
    },
    checkpoints,
    provider_boundary: boundary,
    last_checkpoint_id: lastCheckpoint?.checkpoint_id ?? null,
    last_checkpoint_hash: lastCheckpoint?.checkpoint_hash ?? null,
    next_step_ordinal: providerBoundary || completedStepCount === steps.length
      ? null
      : completedStepCount + 1,
    resume_state: providerBoundary ? 'provider_dispatch_uncertain' : 'resumable',
    resume_allowed: !providerBoundary,
    reason_code: providerBoundary
      ? 'provider_dispatch_already_started'
      : 'workflow_resume_available',
    formal_draft_allowed: false,
  }
}

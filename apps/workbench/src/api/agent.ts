import { fetchRuntimeJson, RuntimeApiError, type RuntimeClientOptions } from './runtime'
import { fetchRuntimeSse } from './sse'
import type { ProviderReasoningEffort } from './providerSettings'

export type AgentAvailability = 'checking' | 'available' | 'unavailable' | 'error'
export type WriterMode = 'repair_only' | 'author_review_change_proposal'
export type AgentRunStatus =
  | 'queued'
  | 'starting'
  | 'running'
  | 'awaiting_approval'
  | 'paused'
  | 'cancelling'
  | 'succeeded'
  | 'failed'
  | 'cancelled'
  | 'interrupted'

export interface AgentStatus {
  schema: 'zhimeng.agent-status.v1'
  available: boolean
  detail: string
  executor: Record<string, unknown> | null
  capabilities: Record<string, unknown> | null
}

export interface ProjectRevision {
  project_id: string
  revision: number
  fingerprint: string
  observed_at: string
}

export interface AgentSession {
  schema: 'zhimeng.agent-session.v1'
  session_id: string
  /** 挂在哪本书下；null = 不绑书的纯对话。书还没有快照时 project_revision 会是空的，别拿它判断归属。 */
  project_id?: string | null
  project_revision: ProjectRevision | null
  title: string
  pinned?: boolean
  status: 'active' | 'idle' | 'closing' | 'closed' | 'interrupted'
  revision: number
  executor: Record<string, unknown>
  capabilities: Record<string, unknown>
  correlation_id: string
  last_run_ordinal: number
  last_message_ordinal: number
  created_at: string
  updated_at: string
}

export interface SessionCheckpointRecord {
  schema: 'zhimeng.session-checkpoint.v1'
  checkpoint_id: string
  session_id: string
  run_id: string | null
  through_ordinal: number
  source_message_count: number
  content: string
  content_hash: string
  reported_model: string | null
  created_at: string
}

export interface AgentProviderPreference {
  providerId?: string
  model: string
  reasoningEffort: ProviderReasoningEffort
}

export function agentSessionProviderPreference(
  session: AgentSession | null | undefined,
): AgentProviderPreference | null {
  const extensions = session?.capabilities?.extensions
  if (!extensions || typeof extensions !== 'object') return null
  const extension = (extensions as Record<string, unknown>)['x-zhimeng.provider-preference']
  if (!extension || typeof extension !== 'object') return null
  const config = (extension as { config?: unknown }).config
  if (!config || typeof config !== 'object') return null
  const model = (config as { model?: unknown }).model
  const providerId = (config as { provider_id?: unknown }).provider_id
  const effort = (config as { reasoning_effort?: unknown }).reasoning_effort
  if (typeof model !== 'string' || !model.trim()) return null
  if (!['auto', 'none', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'].includes(String(effort))) {
    return null
  }
  return {
    providerId: typeof providerId === 'string' && providerId.trim() ? providerId.trim() : undefined,
    model: model.trim(),
    reasoningEffort: effort as ProviderReasoningEffort,
  }
}

export interface AgentMessage {
  schema: 'zhimeng.agent-message.v1'
  message_id: string
  session_id: string
  run_id: string
  ordinal: number
  role: 'system' | 'user' | 'assistant' | 'tool'
  format: 'plain' | 'markdown'
  content: string
  images?: AgentImageAttachment[]
  created_at: string
  /** 这一轮的用量（含缓存命中 cached_input_tokens）；没有就不带。 */
  usage?: {
    input_tokens: number
    output_tokens?: number
    total_tokens?: number
    cached_input_tokens?: number | null
    input_tokens_reported?: boolean
    output_tokens_reported?: boolean
    cached_input_tokens_complete?: boolean
  } | null
  run_usage?: {
    scope: 'main_and_tool_compaction'
    main: { requests: number; completed: number; usage: AgentMessage['usage'] }
    compaction: { requests: number; completed: number; usage: AgentMessage['usage'] }
    total: AgentMessage['usage']
  } | null
}

export interface AgentImageAttachment {
  image_id: string
  name: string
  mime_type: string
  width: number
  height: number
  url: string
}

export interface AgentRun {
  schema: 'zhimeng.execution-run.v1'
  run_id: string
  session_id: string
  ordinal: number
  status: AgentRunStatus
  objective: string
  origin_kind?: 'user' | 'command_completion'
  created_at: string
  updated_at: string
  started_at: string | null
  completed_at: string | null
  executor: Record<string, unknown>
  capabilities: Record<string, unknown>
  project_revision: ProjectRevision | null
  correlation_id: string
  causation_id: string | null
  last_activity_sequence: number
  checkpoint_id: string | null
  error: AgentErrorEnvelope | null
}

export async function getAgentRun(runId: string, options: RuntimeClientOptions = {}): Promise<AgentRun> {
  const { run } = await fetchRuntimeJson<{ run: AgentRun }>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}`, options,
  )
  if (run?.schema !== 'zhimeng.execution-run.v1' || run.run_id !== runId
    || typeof run.session_id !== 'string' || typeof run.objective !== 'string') {
    throw new AgentContractError('来源任务与这条修改记录不一致')
  }
  return run
}

export interface AgentRunContinuation {
  schema: 'zhimeng.agent-run-continuation.v1'
  run_id: string
  can_continue: boolean
  reason_code: string
  reason: string
  stop_reason: string | null
  objective: string
  continued_by_run_id: string | null
  completed_tool_count: number
  uncertain_tool_count: number
}

export async function getAgentRunContinuation(runId: string, options: RuntimeClientOptions = {}, signal?: AbortSignal): Promise<AgentRunContinuation> {
  const value = await fetchRuntimeJson<AgentRunContinuation>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/continuation`,
    { timeoutMs: 30_000, ...options }, { signal },
  )
  if (value?.schema !== 'zhimeng.agent-run-continuation.v1' || value.run_id !== runId
    || typeof value.can_continue !== 'boolean' || typeof value.objective !== 'string'
    || typeof value.reason !== 'string' || typeof value.reason_code !== 'string'
    || !(value.continued_by_run_id === null || typeof value.continued_by_run_id === 'string')
    || !(value.stop_reason === null || typeof value.stop_reason === 'string')
    || !Number.isInteger(value.completed_tool_count) || value.completed_tool_count < 0
    || !Number.isInteger(value.uncertain_tool_count) || value.uncertain_tool_count < 0) {
    throw new AgentContractError('继续任务的状态与当前任务不一致')
  }
  return value
}

export class AgentContractError extends Error {
  constructor(message: string) {
    super(message)
    this.name = 'AgentContractError'
  }
}

export type AgentJsonValue =
  | null
  | boolean
  | number
  | string
  | AgentJsonValue[]
  | { [key: string]: AgentJsonValue }

export interface AgentErrorEnvelope {
  schema?: string
  error: {
    code: string
    message: string
    request_id?: string | null
    retryable: boolean
    // 后端在写入时已脱敏：只有地址/状态码/头名/密钥后四位，绝不含完整密钥。
    details?: Record<string, AgentJsonValue> | null
  }
}

export interface AgentMessagePayload {
  role: 'system' | 'user' | 'assistant' | 'tool'
  content: string | null
  name: string | null
  message_id: string | null
  phase: 'delta' | 'completed'
  delta: string | null
  format: 'plain' | 'markdown'
  finish_reason: string | null
  usage: Record<string, number | boolean | null> | null
}

export interface AgentRunStatePayload {
  status: AgentRunStatus
  previous_status: AgentRunStatus | null
  detail: string | null
}

export interface AgentReasoningPayload extends Record<string, unknown> {
  summary: string
  content: string | null
  visibility: 'summary' | 'full' | 'redacted'
  /** 同一次模型思考的增量和最终快照共享身份；旧活动没有这些字段。 */
  block_id?: string | null
  phase?: 'delta' | 'completed' | 'interrupted'
  delta?: string | null
  source?: 'model' | 'runtime'
}

export interface AgentToolCallPayload {
  tool_call_id: string
  tool_name: string
  arguments: AgentJsonValue
}

export interface AgentToolResultPayload {
  tool_call_id: string
  status: 'succeeded' | 'failed' | 'cancelled'
  output: AgentJsonValue
  error: AgentErrorEnvelope | null
  duration_ms: number | null
}

export interface AgentSkillPermissionSnapshot {
  read: ['bound_project_snapshot']
  write: ['run_staging']
  network: false
  secrets: []
}

export interface AgentSkillReceiptArtifact {
  artifact_type: string
  relative_path: string | null
  content_hash: string
}

export interface AgentSkillReceiptError {
  code: string
  message: string
  retryable: boolean
}

export interface AgentSkillExecutionReceipt {
  schema: 'zhimeng.skill-execution-receipt.v1'
  receipt_id: string
  run_id: string
  tool_call_id: string
  project_id: string
  project_revision: number
  project_fingerprint: string
  skill_id: string
  skill_version: string
  skill_kind: 'deterministic_tool' | 'instruction_source'
  skill_version_id: string
  manifest_fingerprint: string
  package_fingerprint: string
  permissions: AgentSkillPermissionSnapshot
  permissions_hash: string
  input_hash: string
  output_hash: string
  execution_status: 'succeeded' | 'failed' | 'cancelled'
  domain_status: 'healthy' | 'degraded' | 'blocked' | null
  exit_code: number | null
  started_at: string
  finished_at: string
  duration_ms: number
  artifacts: AgentSkillReceiptArtifact[]
  error: AgentSkillReceiptError | null
}

export interface AgentSkillCallOutput {
  receipt: AgentSkillExecutionReceipt
  result_summary: string | null
}

export interface AgentSkillCallPayload {
  skill_call_id: string
  skill_id: string
  skill_version: string | null
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  input: AgentJsonValue
  output: AgentSkillCallOutput | null
}

export interface AgentChangeProposalPayload {
  proposal_id: string
  title: string
  summary: string
  base_revision: number
  base_fingerprint: string
  files: Array<{
    file_id: string | null
    path: string
    operation: 'create' | 'update' | 'delete'
    content_hash_before: string | null
    content_hash_after: string | null
  }>
}

export interface AgentApprovalRequestPayload {
  approval_id: string
  subject_kind: 'tool_call' | 'command' | 'change_proposal' | 'skill_call' | 'subagent'
  subject_id: string
  prompt: string
  options: Array<{
    option_id: string
    label: string
    effect: 'approve' | 'reject' | 'cancel' | 'custom'
  }>
  status: 'pending' | 'resolved' | 'expired' | 'cancelled'
  selected_option_id: string | null
}

export interface AgentArtifactPayload {
  artifact_id: string
  artifact_type: string
  name: string
  uri: string
  media_type: string | null
  content_hash: string | null
  size_bytes: number | null
}

export interface AgentCommandPayload {
  command_id: string
  command: string
  cwd: string | null
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'cancelled'
  exit_code: number | null
  output_artifact_id: string | null
}

export interface AgentErrorPayload {
  error: AgentErrorEnvelope
  fatal: boolean
  scope: 'activity' | 'run' | 'session' | 'runtime'
}

interface AgentActivityBase {
  schema: 'zhimeng.activity-item.v1'
  activity_id: string
  session_id: string
  run_id: string
  sequence: number
  created_at: string
  project_revision: ProjectRevision | null
  correlation_id: string
  causation_id: string | null
}

export type AgentActivity = AgentActivityBase & (
  | { kind: 'message'; payload: AgentMessagePayload }
  | { kind: 'run_state'; payload: AgentRunStatePayload }
  | { kind: 'reasoning'; payload: AgentReasoningPayload }
  | { kind: 'tool_call'; payload: AgentToolCallPayload }
  | { kind: 'tool_result'; payload: AgentToolResultPayload }
  | { kind: 'skill_call'; payload: AgentSkillCallPayload }
  | { kind: 'change_proposal'; payload: AgentChangeProposalPayload }
  | { kind: 'approval_request'; payload: AgentApprovalRequestPayload }
  | { kind: 'artifact'; payload: AgentArtifactPayload }
  | { kind: 'command'; payload: AgentCommandPayload }
  | { kind: 'error'; payload: AgentErrorPayload }
  | {
      kind: 'subagent' | 'compaction'
      payload: Record<string, unknown>
    }
)

export interface AgentRunEvent {
  schema: 'zhimeng.run-event.v1'
  event_id: string
  event_type: 'activity' | 'done'
  emitted_at: string
  data: { activity?: AgentActivity; run?: AgentRun }
}

interface SessionListResponse {
  sessions: AgentSession[]
}

export interface AgentRunProcessSummary {
  has_process: boolean
  tool_count: number
}

export interface AgentMessagePage {
  run_origins?: Record<string, 'user' | 'command_completion'>
  review_sources?: Record<string, AgentReviewSource>
  messages: AgentMessage[]
  last_ordinal: number
  /** 这一页前面还有更早的消息。 */
  has_earlier?: boolean
  /** Missing means unknown (older runtime); false means no persisted process. */
  run_process_summaries?: Record<string, AgentRunProcessSummary>
}

interface RunListResponse {
  runs: AgentRun[]
}

export interface AgentActivityPage {
  schema: 'zhimeng.agent-activity-list.v1'
  run_id: string
  activities: AgentActivity[]
  last_sequence: number
  next_after_sequence?: number | null
  last_event_sequence?: number | null
}

export interface RunCreateResponse {
  run: AgentRun
  user_message: AgentMessage
  replayed: boolean
}

function jsonRequest(body: unknown, method = 'POST', signal?: AbortSignal): RequestInit {
  return {
    method,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
    signal,
  }
}

export function getAgentStatus(
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentStatus> {
  return fetchRuntimeJson('/api/v1/agent/status', options, { signal })
}

export async function listAgentSessions(
  projectId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession[]> {
  const payload = await fetchRuntimeJson<SessionListResponse>(
    `/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
    options,
    { signal },
  )
  return payload.sessions
}

export function createAgentSession(
  projectId: string,
  title: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/projects/${encodeURIComponent(projectId)}/agent/sessions`,
    options,
    jsonRequest({ title }, 'POST', signal),
  )
}

export async function listUnboundAgentSessions(
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession[]> {
  const payload = await fetchRuntimeJson<SessionListResponse>(
    '/api/v1/agent/sessions',
    options,
    { signal },
  )
  return payload.sessions
}

/** 全部对话：绑在各部小说下的 + 不绑书的纯对话。界面左栏按小说分组渲染要用。 */
export async function listAllAgentSessions(
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession[]> {
  const payload = await fetchRuntimeJson<SessionListResponse>(
    '/api/v1/agent/sessions?scope=all',
    options,
    { signal },
  )
  return payload.sessions
}

/** 把这条对话改挂到哪个项目下；projectId 传 null 就变回不针对任何项目的纯对话。 */
export function rebindAgentSessionProject(
  sessionId: string,
  projectId: string | null,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/book`,
    options,
    jsonRequest({ project_id: projectId }, 'PUT', signal),
  )
}

export function createUnboundAgentSession(
  title: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    '/api/v1/agent/sessions',
    options,
    jsonRequest({ title }, 'POST', signal),
  )
}

export function getAgentSession(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}`,
    options,
    { signal },
  )
}

export function updateAgentSession(
  sessionId: string,
  changes: {
    title?: string
    pinned?: boolean
    rebind_provider?: boolean
    provider_id?: string
    model?: string
    reasoning_effort?: ProviderReasoningEffort
  },
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}`,
    options,
    jsonRequest(changes, 'PATCH', signal),
  )
}

export function deleteAgentSession(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<void> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}`,
    options,
    { method: 'DELETE', signal },
  )
}

export function forkAgentSession(
  sessionId: string,
  upToMessageId: string,
  title?: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentSession> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/fork`,
    options,
    jsonRequest({ up_to_message_id: upToMessageId, title }, 'POST', signal),
  )
}

export function compactAgentSession(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<SessionCheckpointRecord> {
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/compact`,
    { timeoutMs: 120_000, ...options },
    { method: 'POST', signal },
  )
}

export async function getLatestSessionCheckpoint(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<SessionCheckpointRecord | null> {
  return fetchRuntimeJson<SessionCheckpointRecord | null>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/checkpoint`,
    options,
    { signal },
  )
}

/** 取最新的 500 条（长对话打开要看的是最后那些）；给了 beforeOrdinal 就取它前面最新的 500 条。 */
export async function listAgentMessages(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
  beforeOrdinal?: number,
): Promise<AgentMessagePage> {
  const before = beforeOrdinal === undefined ? '' : `&before_ordinal=${beforeOrdinal}`
  return fetchRuntimeJson<AgentMessagePage>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/messages?limit=500&latest=true${before}`,
    options,
    { signal },
  )
}

export async function listAgentRuns(
  sessionId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRun[]> {
  const payload = await fetchRuntimeJson<RunListResponse>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs?limit=100`,
    options,
    { signal },
  )
  return payload.runs
}

export async function listAgentActivities(
  runId: string,
  afterSequence = 0,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentActivityPage> {
  const activities: AgentActivity[] = []
  let cursor = afterSequence
  let upper: number | undefined
  let eventCursor: number | null | undefined
  while (true) {
    signal?.throwIfAborted()
    const bound = upper === undefined ? '' : `&through_sequence=${upper}`
    const page = await fetchRuntimeJson<AgentActivityPage>(
      `/api/v1/agent/runs/${encodeURIComponent(runId)}/activities?after_sequence=${cursor}&limit=250${bound}`,
      options, { signal },
    )
    signal?.throwIfAborted()
    if (page.run_id !== runId || (upper !== undefined && page.last_sequence !== upper)
      || (upper !== undefined && page.last_event_sequence !== eventCursor)) throw new Error('处理记录的轮次或范围发生变化，请重试读取。')
    upper = page.last_sequence
    eventCursor = page.last_event_sequence
    for (const activity of page.activities) {
      if (activity.run_id !== runId || activity.sequence <= cursor || activity.sequence > upper) throw new Error('处理记录顺序异常，请重试读取。')
      activities.push(activity)
      cursor = activity.sequence
    }
    if (page.next_after_sequence == null) return { ...page, activities }
    if (page.activities.length === 0 || page.next_after_sequence !== cursor || cursor >= upper) {
      throw new Error('处理记录分页没有向后推进，请重试读取。')
    }
  }
}

export interface AgentReviewSource {
  source_run_id: string
  source_run_ordinal: number
  source_message_id: string | null
}

export interface AgentRunRequest {
    goal?: import('./sessionGoal').GoalAdmission
    message: string
    expected_project_revision?: number
    expected_project_fingerprint?: string
    /** 用户这时在右栏开着的文件（打开的文件夹里的相对路径）。 */
    open_file_path?: string
    writer_mode: WriterMode
    context_pack_id?: string
    expected_context_pack_hash?: string
    /** 用户亲手附上的文件（打开文件夹的对话）：这一轮后端把内容带给模型。 */
    manual_file_ids?: string[]
    continuation_source_run_id?: string | null
    review_source_run_id?: string | null
    manual_skill_names?: string[]
    image_ids?: string[]
}

export function createAgentRun(
  sessionId: string,
  request: AgentRunRequest,
  idempotencyKey: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<RunCreateResponse> {
  const init = jsonRequest(request, 'POST', signal)
  const headers = new Headers(init.headers)
  headers.set('Idempotency-Key', idempotencyKey)
  return fetchRuntimeJson(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs`,
    options,
    { ...init, headers },
  )
}

/** 按发送身份对账；404 只表示查询这一刻还未受理，重试仍用原 key。 */
export async function getAgentRunAdmission(
  sessionId: string,
  idempotencyKey: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<RunCreateResponse | null> {
  try {
    const value = await fetchRuntimeJson<RunCreateResponse>(
      `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs/admission`,
      options,
      { headers: { 'Idempotency-Key': idempotencyKey }, cache: 'no-store', signal: signal ?? new AbortController().signal },
    )
    if (value?.run?.session_id !== sessionId || value.user_message?.session_id !== sessionId
      || value.user_message?.run_id !== value.run?.run_id) {
      throw new AgentContractError('发送结果与当前对话不一致')
    }
    return value
  } catch (error) {
    if (error instanceof RuntimeApiError && error.status === 404
      && (error.payload as { error?: { code?: string } })?.error?.code === 'agent_admission_not_found') return null
    throw error
  }
}

export async function withdrawAgentRunAdmission(
  sessionId: string,
  idempotencyKey: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<{ state: 'accepted'; accepted: RunCreateResponse } | { state: 'withdrawn'; accepted: null }> {
  const value = await fetchRuntimeJson<{ state: 'accepted'; accepted: RunCreateResponse } | { state: 'withdrawn'; accepted: null }>(
    `/api/v1/agent/sessions/${encodeURIComponent(sessionId)}/runs/admission/withdraw`, options,
    { method: 'POST', headers: { 'Idempotency-Key': idempotencyKey }, cache: 'no-store', signal },
  )
  if (value?.state === 'withdrawn' && value.accepted === null) return value
  if (value?.state === 'accepted' && value.accepted?.run?.session_id === sessionId
    && value.accepted.user_message?.session_id === sessionId
    && value.accepted.user_message.run_id === value.accepted.run.run_id) return value
  throw new AgentContractError('取回发送的结果与当前对话不一致')
}

export async function cancelAgentRun(
  runId: string,
  options: RuntimeClientOptions = {},
  signal?: AbortSignal,
): Promise<AgentRun> {
  const payload = await fetchRuntimeJson<{ run: AgentRun }>(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/cancel`,
    options,
    { method: 'POST', signal },
  )
  return payload.run
}

export function streamAgentRunEvents(
  runId: string,
  options: RuntimeClientOptions & {
    signal: AbortSignal
    lastEventId?: string | null
    onEvent: (event: AgentRunEvent) => void
  },
): Promise<void> {
  return fetchRuntimeSse(
    `/api/v1/agent/runs/${encodeURIComponent(runId)}/events`,
    {
      ...options,
      onEvent: (event) => {
        const payload = JSON.parse(event.data) as AgentRunEvent
        if (
          payload.schema !== 'zhimeng.run-event.v1' ||
          !['activity', 'done'].includes(payload.event_type)
        ) {
          throw new Error('Runtime emitted an invalid Agent event')
        }
        options.onEvent(payload)
      },
    },
  )
}

export function isTerminalAgentRun(status: AgentRunStatus): boolean {
  return ['succeeded', 'failed', 'cancelled', 'interrupted'].includes(status)
}

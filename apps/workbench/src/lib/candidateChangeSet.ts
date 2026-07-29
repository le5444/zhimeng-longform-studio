import type { AgentCandidateOutput } from '../api/agent'
import type { ChangeSet } from '../api/changeSets'

export function candidateBoundChangeSet(
  candidate: AgentCandidateOutput | null,
  candidateChangeSetId: string | null,
  detail: ChangeSet | null,
): ChangeSet | null {
  if (!candidate || !detail) return detail
  if (detail.project_id !== candidate.project_id) return null
  if (candidateChangeSetId !== detail.change_set_id) return null
  return detail.source_run_id === candidate.run_id ? detail : null
}

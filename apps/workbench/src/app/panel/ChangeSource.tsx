import { useEffect, useState } from 'react'
import { getAgentRun, type AgentRun } from '../../api/agent'
import { authorFacingError } from '../../lib/authorFacingError'
import { Spinner } from '../ui'

// A different record or scope must never show the previous task's requirements.
export function ChangeSource(props: { runId: string | null; sessionId?: string; projectId?: string }) {
  if (!props.runId) return null
  return <SourceReader key={`${props.runId}:${props.sessionId ?? ''}:${props.projectId ?? ''}`} {...props} runId={props.runId} />
}

function SourceReader({ runId, sessionId, projectId }: { runId: string; sessionId?: string; projectId?: string }) {
  const [open, setOpen] = useState(false)
  const [retry, setRetry] = useState(0)
  const [result, setResult] = useState<{ run: AgentRun | null; error: string | null } | null>(null)
  useEffect(() => {
    if (!open) return
    const controller = new AbortController()
    setResult(null)
    void getAgentRun(runId, { signal: controller.signal }).then((run) => {
      if ((sessionId && run.session_id !== sessionId) || (projectId && run.project_revision?.project_id !== projectId)) {
        throw new Error('来源任务不属于这条修改记录所在的对话或项目。')
      }
      if (!controller.signal.aborted) setResult({ run, error: null })
    }).catch((error: unknown) => {
      if (!controller.signal.aborted) setResult({ run: null, error: authorFacingError(error) })
    })
    return () => controller.abort()
  }, [open, runId, sessionId, projectId, retry])

  return <details className="zm-change-source" open={open} onToggle={(event) => setOpen(event.currentTarget.open)}>
    <summary>来源任务</summary>
    {open ? <>
      {!result ? <p className="zm-muted"><Spinner /> 正在读取来源任务…</p> : null}
      {result?.error ? <p className="zm-error-line" role="alert">{result.error} <button type="button" className="zm-link" onClick={() => setRetry((value) => value + 1)}>重试</button></p> : null}
      {result?.run ? <><p className="zm-muted">{result.run.origin_kind === 'command_completion' ? '命令结束后的接续要求' : '初始要求'}</p><pre>{result.run.objective}</pre></> : null}
    </> : null}
  </details>
}

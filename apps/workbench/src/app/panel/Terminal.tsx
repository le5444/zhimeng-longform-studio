import { useEffect, useRef, useState } from 'react'
import { Terminal as XTerminal } from '@xterm/xterm'
import { FitAddon } from '@xterm/addon-fit'
import { inputCommandSession, readCommandSession, resizeCommandSession, stopCommandSession, type CommandSessionSummary } from '../../api/commandSessions'
import { Btn } from '../ui'
import '@xterm/xterm/css/xterm.css'
import './terminal.css'

export default function Terminal({ sessionId, summary, active, onObserved }: {
  sessionId: string
  summary: CommandSessionSummary
  active: boolean
  onObserved: (command: CommandSessionSummary) => void
}) {
  const container = useRef<HTMLDivElement>(null)
  const observed = useRef(onObserved)
  observed.current = onObserved
  const latest = useRef(summary)
  latest.current = summary
  const send = useRef<(text: string) => void>(() => {})
  const [ready, setReady] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [inputError, setInputError] = useState<string | null>(null)
  const [diagnostics, setDiagnostics] = useState('')
  const [reconnect, setReconnect] = useState(0)
  const [stopping, setStopping] = useState(false)
  const commandId = summary.command_session_id

  useEffect(() => {
    if (!active || !container.current) return
    const controller = new AbortController()
    const term = new XTerminal({ cols: latest.current.terminal_columns ?? 80, rows: latest.current.terminal_rows ?? 24,
      cursorBlink: true, fontFamily: 'Consolas, monospace', fontSize: 13, scrollback: 10000,
      windowsPty: { backend: 'conpty', buildNumber: latest.current.terminal_windows_build ?? undefined },
      screenReaderMode: true, disableStdin: true, theme: { background: '#14171c', foreground: '#e6e8ec' } })
    const fit = new FitAddon()
    term.loadAddon(fit)
    term.open(container.current)
    let timer: ReturnType<typeof setTimeout> | undefined
    let resizeTimer: ReturnType<typeof setTimeout> | undefined
    let accepting = false, failedInput = false, running = latest.current.status === 'running'
    let offsets = { stdout_offset: 0, stderr_offset: 0 }
    let pending = Promise.resolve()
    let requestedSize = ''
    setReady(false); setError(null); setInputError(null); setDiagnostics('')

    const enqueue = (operation: () => Promise<CommandSessionSummary>) => {
      pending = pending.then(async () => {
        if (controller.signal.aborted || failedInput || !running) return
        try {
          const next = await operation()
          if (!controller.signal.aborted) observed.current(next)
        } catch (reason) {
          if (controller.signal.aborted) return
          failedInput = true
          term.options.disableStdin = true
          setInputError(reason instanceof Error ? reason.message : '输入或尺寸变更未确认')
        }
      })
    }
    const input = (text: string) => {
      if (accepting && running && !failedInput) enqueue(() => inputCommandSession(sessionId, commandId, text, { signal: controller.signal }))
    }
    send.current = input
    const onData = term.onData(input)
    const sizes = term.parser.registerCsiHandler({ final: 't' }, params => {
      if (params[0] !== 8 || typeof params[1] !== 'number' || typeof params[2] !== 'number' || params[1] < 1 || params[2] < 1 || params[1] > 32767 || params[2] > 32767) return false
      term.resize(params[2], params[1])
      return true
    })
    const fitTerminal = () => {
      const size = fit.proposeDimensions()
      if (!size || !accepting || !Number.isInteger(size.cols) || !Number.isInteger(size.rows)
        || size.cols < 1 || size.rows < 1 || size.cols > 32767 || size.rows > 32767) return
      if (!running) { term.resize(size.cols, size.rows); return }
      const key = `${size.rows}:${size.cols}`
      if (requestedSize === key || failedInput) return
      requestedSize = key
      // The persisted size marker changes xterm after earlier output has been
      // consumed. Resizing it immediately can parse old 80-column frames at 59.
      enqueue(() => resizeCommandSession(sessionId, commandId, size.rows, size.cols, { signal: controller.signal }))
    }
    const resize = new ResizeObserver(() => {
      clearTimeout(resizeTimer)
      resizeTimer = setTimeout(() => { if (accepting && !controller.signal.aborted) fitTerminal() }, 100)
    })
    resize.observe(container.current)
    const poll = async () => {
      try {
        const next = await readCommandSession(sessionId, commandId, { ...offsets, max_bytes: 65536 }, { signal: controller.signal })
        if (controller.signal.aborted) return
        // Start at byte zero when attaching. Replaying history only renders it;
        // xterm's historical device replies must never become new process input.
        if (next.stdout.text) await new Promise<void>(resolve => term.write(next.stdout.text, resolve))
        if (controller.signal.aborted) return
        offsets = { stdout_offset: next.stdout.next_offset, stderr_offset: next.stderr.next_offset }
        if (next.stderr.text) setDiagnostics(value => value + next.stderr.text)
        running = next.status === 'running'
        observed.current(next)
        setError(null)
        if (!next.stdout.has_more && !next.stderr.has_more) {
          if (!accepting) {
            accepting = true
            fitTerminal()
          }
          setReady(true)
          term.options.disableStdin = !running || failedInput
        }
        if (running || next.stdout.has_more || next.stderr.has_more) timer = setTimeout(() => void poll(), next.stdout.has_more || next.stderr.has_more ? 0 : 250)
      } catch (reason) {
        if (controller.signal.aborted) return
        accepting = false
        term.options.disableStdin = true
        setReady(false)
        setError(reason instanceof Error ? reason.message : '无法读取终端输出')
      }
    }
    void poll()
    return () => {
      controller.abort(); clearTimeout(timer); clearTimeout(resizeTimer)
      send.current = () => {}
      resize.disconnect(); onData.dispose(); sizes.dispose(); term.dispose()
    }
  }, [sessionId, commandId, active, reconnect])

  const stop = async () => {
    setStopping(true)
    try {
      const next = await stopCommandSession(sessionId, commandId)
      observed.current(next)
    } catch (reason) { setInputError(reason instanceof Error ? reason.message : '停止未确认，请核对输出后重试') }
    finally { setStopping(false) }
  }
  return <div className="zm-command-detail zm-terminal-detail">
    <div className="zm-command-detail-head">
      <strong>{summary.status === 'running' ? '交互终端 · 运行中' : summary.status === 'interrupted' ? '交互终端 · 已中断' : summary.status === 'cancelled' ? '交互终端 · 已停止' : `交互终端 · 退出码 ${summary.exit_code ?? '未记录'}`}</strong>
      {summary.status === 'running' ? <div className="zm-terminal-actions">
        <Btn size="sm" variant="ghost" disabled={!ready || Boolean(inputError)} onClick={() => send.current('\u0003')}>Ctrl+C</Btn>
        <Btn size="sm" variant="ghost" busy={stopping} onClick={() => void stop()}>停止命令</Btn>
      </div> : null}
    </div>
    <details className="zm-command-metadata"><summary>命令详情</summary><pre>{summary.command}</pre><p>{summary.cwd}</p></details>
    {summary.interruption_reason ? <p className="zm-command-note">{summary.interruption_reason}</p> : null}
    {error || inputError ? <div role="alert" className="zm-command-error">{error || inputError}。未确认的输入不会自动重发，请核对输出。
      <Btn size="sm" onClick={() => setReconnect(value => value + 1)}>重新连接终端</Btn>
    </div> : null}
    <div ref={container} className="zm-terminal" aria-label="交互终端" />
    <p className="zm-command-note">{!ready ? '正在恢复终端输出…' : summary.status === 'running' ? '点击终端后可输入；Ctrl+C 中断当前程序，停止命令会结束此命令的进程。' : '终端已结束，输出仍可查看。'}</p>
    {diagnostics ? <details><summary>终端连接诊断</summary><pre>{diagnostics}</pre></details> : null}
  </div>
}

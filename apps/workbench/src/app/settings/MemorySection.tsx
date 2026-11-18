import { SettingsTabs, SettingsPane } from './navigation'
import { useSettingsPane } from './useSettingsPane'
import { useCallback, useEffect, useRef, useState } from 'react'
import { readMemoryHistory, readMemoryVersion, readUserMemory, saveMemoryEnabled, saveUserMemory, type MemoryHistory, type UserMemoryView } from '../../api/userMemory'
import { History, Pencil, Plus, RefreshCw, RotateCcw, Search, Trash2 } from 'lucide-react'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { ST } from './settings.text'
import { IconBtn, Switch, type Notify } from '../ui'
import { Button, Input, Panel, Textarea } from './kit'
import { S } from './settingsText'
import { useSettingsDraft } from './drafts'

/** 《用户记忆.md》里的一条：第几行、给人看的字、行首的「- 」这类记号（改的时候原样留着）。 */
export interface MemoryEntry {
  line: number
  text: string
  prefix: string
}

/** 一行一条；空行不算，行首的「- 」「* 」只是记号，不算内容。 */
export function memoryEntries(content: string): MemoryEntry[] {
  return content.split('\n').flatMap((raw, line) => {
    const match = /^(\s*(?:[-*]\s+)?)(.*)$/.exec(raw.replace(/\r$/, ''))
    const text = match?.[2].trim() ?? ''
    return text ? [{ line, text, prefix: match?.[1] ?? '' }] : []
  })
}

/** 改第 line 行（text 为 null 就删掉这一行），别的行一个字不动。 */
export function replaceMemoryLine(content: string, entry: MemoryEntry, text: string | null): string {
  const lines = content.split('\n')
  if (text === null) {
    lines.splice(entry.line, 1)
  } else {
    const ending = lines[entry.line]?.endsWith('\r') ? '\r' : ''
    lines[entry.line] = `${entry.prefix}${text}${ending}`
  }
  return lines.join('\n')
}

export function appendMemoryLine(content: string, text: string): string {
  if (!content.trim()) return `${text}\n`
  return `${content.replace(/\s+$/, '')}\n${text}\n`
}

/**
 * 设置 → 记忆（9-29，作者：「agent根据我对话直接记忆，可以开关和编辑，但是不能只有编辑」）。
 *
 * 一个用户记忆开关（开着自动记录、每个对话开头带上；关了暂停用户记忆），项目说明单独沿用文件权限。
 * 下面一条一条列出来，能改、能删、能自己加一条；要大改就展开整份直接改。
 * 存的时候带上读到时的指纹：这中间 Agent 又记了一条，后台回 409，这里重读一遍，不把它盖掉。
 */
const MEMORY_DESTINATIONS = { 'memory-content': 'entries', 'memory-history': 'history', 'memory-raw': 'raw' } as const

export function MemoryTab({ notify }: { notify: Notify }) {
  const request = useRef(0)
  const operation = useRef(false)
  const alive = useRef(true)
  useEffect(() => { alive.current = true; return () => { alive.current = false } }, [])
  const [view, setView] = useState<UserMemoryView | null>(null)
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [busy, setBusy] = useState<string | null>(null)
  const [feedback, setFeedback] = useState<{ error: boolean; text: string } | null>(null)
  const [editing, setEditing] = useState<{ line: number; text: string; base: UserMemoryView; entry: MemoryEntry } | null>(null)
  const [adding, setAdding] = useState('')
  const [raw, setRaw] = useState<{ content: string; sha256: string } | null>(null)
  const [query, setQuery] = useState('')
  const [pane, setPane] = useSettingsPane('entries', MEMORY_DESTINATIONS)
  useEffect(() => {
    if (pane === 'raw' && view && !raw) setRaw({ content: view.content, sha256: view.sha256 })
    else if (pane !== 'raw' && raw && raw.content === view?.content) setRaw(null)
  }, [pane, view, raw])
  useSettingsDraft('memory', Boolean(adding.trim() || (editing && editing.text !== editing.entry.text) || (raw && raw.content !== view?.content)))

  const refresh = useCallback(async () => {
    const owner = ++request.current
    try {
      const next = await readUserMemory()
      if (!alive.current || owner !== request.current) return
      setView(next)
      setState('ready')
    } catch (error) {
      if (!alive.current || owner !== request.current) return
      setState('error')
      notify('danger', authorFacingError(error, ST.memoryLoadFailed))
    }
  }, [notify])

  useEffect(() => { void refresh() }, [refresh])

  const save = async (key: string, next: string, done: string, expectedSha256 = view?.sha256) => {
    if (!view || operation.current) return false
    operation.current = true
    request.current++
    setBusy(key)
    setFeedback(null)
    try {
      const saved = await saveUserMemory(next, expectedSha256 ?? null)
      if (!alive.current) return false
      setView(saved)
      if (key === 'raw') setRaw({ content: saved.content, sha256: saved.sha256 })
      notify('ok', done)
      setFeedback({ error: false, text: done })
      return true
    } catch (error) {
      if (!alive.current) return false
      setFeedback({ error: true, text: authorFacingError(error, ST.memorySaveFailed) })
      if (error instanceof RuntimeApiError && error.status === 409) {
        notify('warn', authorFacingError(error, ST.memoryChanged))
        await refresh()
      } else {
        notify('danger', authorFacingError(error, ST.memorySaveFailed))
      }
      return false
    } finally {
      operation.current = false
      if (alive.current) setBusy(null)
    }
  }

  const toggle = async () => {
    if (!view || operation.current) return
    operation.current = true
    request.current++
    setBusy('switch')
    setFeedback(null)
    try {
      const next = await saveMemoryEnabled(!view.enabled)
      if (!alive.current) return
      setView(next)
      notify('ok', next.enabled ? ST.memoryTurnedOn : ST.memoryTurnedOff)
      setFeedback({ error: false, text: next.enabled ? ST.memoryTurnedOn : ST.memoryTurnedOff })
    } catch (error) {
      if (!alive.current) return
      setFeedback({ error: true, text: authorFacingError(error, ST.memorySaveFailed) })
      notify('danger', authorFacingError(error, ST.memorySaveFailed))
    } finally {
      operation.current = false
      if (alive.current) setBusy(null)
    }
  }

  const entries = view ? memoryEntries(view.content) : []
  const filtered = entries.filter((entry) => entry.text.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const stale = Boolean(view && ((raw && raw.sha256 !== view.sha256) || (editing && editing.base.sha256 !== view.sha256)))

  return (
    <>
    <div className="zm-memory-workspace">
    <Panel title={S.memory.title} actions={<IconBtn label={S.memory.refresh} icon={<RefreshCw />} disabled={Boolean(busy)} onClick={() => void refresh()} />}>
      {feedback ? <p className={`zm-setting-feedback${feedback.error ? ' is-error' : ''}`} role={feedback.error ? 'alert' : 'status'}>{feedback.text}</p> : null}
      {state === 'loading' ? <p className="zm-faint">{ST.memoryLoading}</p> : null}
      {state === 'error' ? (
        <p className="zm-faint">
          {ST.memoryLoadFailed} <Button small variant="quiet" onClick={() => void refresh()}>{ST.memoryRetry}</Button>
        </p>
      ) : null}
      {view ? (
        <>
          <div className="zm-row zm-memory-switch">
            <div className="zm-row-text">
              <span className="zm-row-label">{ST.memorySwitch}</span>
              {view.settings_error ? <span className="zm-row-hint" role="alert">{view.settings_error}</span> : <span className="zm-row-hint">{view.enabled ? ST.memoryIntroOn : ST.memoryIntroOff}</span>}
            </div>
            <div className="zm-row-control">
              <Switch checked={view.enabled} label={ST.memorySwitch} disabled={Boolean(busy)} onChange={() => void toggle()} />
            </div>
          </div>

          <SettingsTabs id="memory-settings" label="记忆管理" value={pane} onChange={setPane} items={[
            { key: 'entries', label: '逐行编辑', count: entries.length }, { key: 'raw', label: '全文编辑' }, { key: 'history', label: '修改历史' },
          ]} />
          <SettingsPane id="memory-settings" name="entries" active={pane}>
          <div data-setting="memory-content" tabIndex={-1}>
          <p className="zm-faint">这里按文件的非空行显示；多行段落、列表和 Markdown 格式请在全文编辑中修改。</p>
          <div className="zm-memory-toolbar">
            <label className="zm-memory-search"><Search aria-hidden /><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder={S.memory.search} aria-label={S.memory.search} /></label>
            <span className="zm-faint">{S.memory.count(entries.length)}</span>
          </div>

          {stale ? <p className="zm-memory-conflict" role="alert">{S.memory.stale}</p> : null}
          {entries.length === 0 ? <p className="zm-memory-empty">{ST.memoryEmpty}</p> : filtered.length === 0 ? <p className="zm-memory-empty">{S.memory.noMatch}</p> : (
            <ul className="zm-memory-list" aria-label={ST.memoryListLabel}>
              {filtered.map((entry) => (
                <li key={entry.line} className="zm-memory-item">
                  {editing?.line === entry.line ? (
                    <form
                      className="zm-memory-edit"
                      onSubmit={(event) => {
                        event.preventDefault()
                        const text = editing.text.trim()
                        if (!text) return
                        void save(`edit:${entry.line}`, replaceMemoryLine(editing.base.content, editing.entry, text), ST.memoryEdited, editing.base.sha256)
                          .then((ok) => { if (ok) setEditing(null) })
                      }}
                    >
                      <Input
                        value={editing.text}
                        disabled={Boolean(busy)}
                        aria-label={ST.memoryEditLabel}
                        autoFocus
                        onChange={(event) => setEditing({ ...editing, text: event.target.value })}
                        onKeyDown={(event) => { if (event.key === 'Escape') setEditing(null) }}
                      />
                      <Button small variant="primary" type="submit" disabled={Boolean(busy) || stale} busy={busy === `edit:${entry.line}`}>{ST.memorySave}</Button>
                      <Button small variant="quiet" disabled={Boolean(busy)} onClick={() => setEditing(null)}>{ST.memoryCancel}</Button>
                    </form>
                  ) : (
                    <>
                      <span className="zm-memory-text">{entry.text}</span>
                      <span className="zm-memory-actions">
                        <IconBtn size="sm" label={ST.memoryEditOne(entry.text)} icon={<Pencil />} disabled={Boolean(busy || raw || editing)} onClick={() => setEditing({ line: entry.line, text: entry.text, entry, base: view })} />
                        <IconBtn
                          size="sm"
                          label={ST.memoryDeleteOne(entry.text)}
                          icon={<Trash2 />}
                          disabled={Boolean(busy || raw || editing)}
                          onClick={() => void save(`delete:${entry.line}`, replaceMemoryLine(view.content, entry, null), ST.memoryDeleted)}
                        />
                      </span>
                    </>
                  )}
                </li>
              ))}
            </ul>
          )}

          <form
            className="zm-memory-add"
            onSubmit={(event) => {
              event.preventDefault()
              const text = adding.trim()
              if (!text) return
              void save('add', appendMemoryLine(view.content, text), ST.memoryAdded).then((ok) => { if (ok) setAdding('') })
            }}
          >
            <Input disabled={Boolean(busy)} value={adding} placeholder={ST.memoryAddPlaceholder} aria-label={ST.memoryAddPlaceholder} onChange={(event) => setAdding(event.target.value)} />
            <Button type="submit" icon={<Plus />} busy={busy === 'add'} disabled={!adding.trim() || Boolean(busy || raw || editing)}>{ST.memoryAdd}</Button>
          </form>

          </div></SettingsPane>
          <SettingsPane id="memory-settings" name="raw" active={pane}>
          <div className="zm-memory-raw" data-setting="memory-raw" tabIndex={-1}>
            <div className="zm-card-title"><h3>{ST.memoryRawToggle}</h3></div>
            <p className="zm-faint">直接编辑整份记忆；切换页面会保留草稿，保存前会检查文件是否被其他任务修改。</p>
          <div className="zm-memory-source"><span>{S.memory.savedFile}</span><code>{view.path}</code>{view.updated_at ? <time dateTime={view.updated_at}>{S.memory.updated} {new Date(view.updated_at).toLocaleString()}</time> : null}</div>
            {stale ? <p className="zm-memory-conflict" role="alert">{S.memory.stale}</p> : null}
            {raw !== null ? (
              <>
                <Textarea disabled={Boolean(busy)} rows={18} value={raw.content} aria-label={ST.memoryRawLabel} onChange={(event) => setRaw({ ...raw, content: event.target.value })} />
                <div className="zm-btn-row">
                  <Button variant="primary" busy={busy === 'raw'} disabled={raw.content === view.content || Boolean(busy || editing || adding.trim()) || stale} onClick={() => void save('raw', raw.content, ST.memorySaved, raw.sha256)}>
                    {ST.memorySave}
                  </Button>
                  <Button disabled={Boolean(busy)} onClick={() => setRaw({ content: view.content, sha256: view.sha256 })}>{stale ? S.memory.reload : S.memory.discard}</Button>
                </div>
              </>
            ) : null}
          </div></SettingsPane>
          <SettingsPane id="memory-settings" name="history" active={pane}>
            <div data-setting="memory-history" tabIndex={-1}><MemoryHistoryPanel key={view.sha256} sha256={view.sha256} disabled={Boolean(busy || raw || editing || adding.trim())} notify={notify} onRestore={(content, sha) => save('restore', content, S.memory.restored, sha)} /></div>
          </SettingsPane>
          <p className="zm-faint">{ST.memoryProjectNote(view.project_memory_file)}</p>
        </>
      ) : null}
    </Panel>
    </div>

    </>
  )
}

function MemoryHistoryPanel({ sha256, disabled, notify, onRestore }: { sha256: string; disabled: boolean; notify: Notify; onRestore: (content: string, sha: string) => Promise<boolean> }) {
  const [history, setHistory] = useState<MemoryHistory | null>(null)
  const [busy, setBusy] = useState(false)
  const [preview, setPreview] = useState<{ version: string; content: string; sha256: string } | null>(null)
  const load = async (offset = 0) => {
    setBusy(true)
    try {
      const result = await readMemoryHistory(offset)
      setHistory((before) => ({ ...result, items: offset ? [...before?.items ?? [], ...result.items] : result.items }))
    } catch (error) { notify('danger', authorFacingError(error, S.memory.historyError)) }
    finally { setBusy(false) }
  }
  return <Panel title={S.memory.history} actions={<Button small icon={<History />} busy={busy} onClick={() => void load()}>{S.memory.loadHistory}</Button>}>
    {history?.items.length === 0 ? <p className="zm-faint">{S.memory.historyEmpty}</p> : null}
    {history ? <ul className="zm-memory-history">
      {history.items.map((item) => <li key={item.version}>
        <div className="zm-memory-history-row"><div><time dateTime={item.saved_at}>{new Date(item.saved_at).toLocaleString()}</time><span>{S.memory.previous} · {S.memory.chars(item.characters)}</span></div>
          <Button small busy={busy} onClick={() => {
            setBusy(true)
            void readMemoryVersion(item.version).then((value) => setPreview({ ...value, sha256 }), (error) => notify('danger', authorFacingError(error, S.memory.historyError))).finally(() => setBusy(false))
          }}>{S.memory.preview}</Button>
        </div>
        {preview?.version === item.version ? <div className="zm-memory-history-preview"><pre>{preview.content || S.memory.emptyVersion}</pre><Button small icon={<RotateCcw />} disabled={disabled || busy} onClick={() => void onRestore(preview.content, preview.sha256)}>{S.memory.restore}</Button></div> : null}
      </li>)}
    </ul> : null}
    {history?.next_offset != null ? <Button small busy={busy} onClick={() => void load(history.next_offset!)}>{S.memory.more}</Button> : null}
  </Panel>
}

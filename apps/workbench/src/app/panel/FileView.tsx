import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type RefObject } from 'react'
import { ArrowLeft, Check, Copy, FileText, MessageSquarePlus, Quote } from 'lucide-react'
import { applyChangeSetDirect, createProjectChangeSet, getChangeSet, listProjectChangeSets, type ChangeSet } from '../../api/changeSets'
import { readBoundProjectFile, type ProjectFile, type ProjectFileContent } from '../../api/projects'
import { authorFacingError } from '../../lib/authorFacingError'
import { Markdown } from '../chat/Markdown'
import { A } from '../text'
import { Btn, IconBtn, type Notify } from '../ui'
import { fileTextSelection, focusFileEditor, scrollFilePoint, type FileFocus } from '../lib/filePosition'
import type { FileDraft, PendingFileSave } from '../lib/fileDrafts'
import { selectionLocation, type FileQuotation } from '../lib/fileQuotation'

function baseName(path: string): string {
  return path.split(/[\\/]/).filter(Boolean).at(-1) ?? path
}

function isMarkdown(path: string): boolean {
  return /\.(md|markdown)$/i.test(path)
}

function countLines(text: string): number {
  if (!text) return 0
  const lines = text.split(/\r\n|\r|\n/)
  return lines.length - (lines[lines.length - 1] === '' ? 1 : 0)
}

export function TextPreview({ text, path, wrap, focus, textRef }: { text: string; path: string; wrap: boolean; focus?: FileFocus; textRef: RefObject<HTMLElement | null> }) {
  const numbers = useMemo(() => Array.from({ length: Math.max(1, countLines(text)) }, (_, index) => index + 1).join('\n'), [text])
  const selection = useMemo(() => focus?.location ? fileTextSelection(text, focus.location) : null, [text, focus])
  const marker = useRef<HTMLElement | null>(null)
  useLayoutEffect(() => {
    const mark = marker.current
    const region = mark?.closest<HTMLElement>('.zm-file-code')
    if (!mark || !region) return
    const point = mark.getClientRects()[0] ?? mark.getBoundingClientRect()
    scrollFilePoint(mark, point.top)
    region.scrollLeft += point.left - region.getBoundingClientRect().left - region.clientWidth / 3
    region.focus({ preventScroll: true })
  }, [selection, focus, wrap])
  if (!text) return <p className="zm-file-empty">空文件</p>
  return (
    <div className={`zm-file-code${wrap ? ' is-wrapped' : ''}`} role="region" aria-label={`文件内容 ${path}`} tabIndex={0}>
      <div className="zm-file-code-inner">
        {!wrap ? <pre className="zm-file-line-numbers" aria-hidden="true">{numbers}</pre> : null}
        <pre className="zm-file-text"><code ref={textRef}>{selection ? <>{text.slice(0, selection.start)}<mark ref={marker} className="zm-file-location">{text.slice(selection.start, selection.end)}</mark>{text.slice(selection.end)}</> : text}</code></pre>
      </div>
    </div>
  )
}

function verifyManualSave(candidate: ChangeSet, content: ProjectFileContent, expectedAfter: string): ChangeSet {
  const matches = candidate.project_id === content.project_id
    && candidate.origin === 'manual'
    && candidate.base_revision === content.snapshot_revision
    && candidate.base_fingerprint === content.project_fingerprint
    && candidate.changes.length === 1
    && candidate.changes[0].file_id === content.file.file_id
    && candidate.changes[0].proposed_after_sha256 === expectedAfter
  if (!matches) throw new Error('原保存记录的内容已经不同。草稿仍保留，请重新读取文件后再核对。')
  return candidate
}

async function recoverManualSave(created: ChangeSet, content: ProjectFileContent): Promise<ChangeSet> {
  const expectedAfter = created.changes[0].proposed_after_sha256!
  if (created.status === 'applied') return verifyManualSave(created, content, expectedAfter)
  const record = (await listProjectChangeSets(content.project_id)).find((item) => item.change_set_id === created.change_set_id)
  if (!record) throw new Error('保存回执尚未确认。原修改记录暂时读不到，草稿仍保留；请稍后重试，不要重复新建。')
  const found = await getChangeSet(created.change_set_id)
  return verifyManualSave(found, content, expectedAfter)
}

/** 你在文件页改的：建一张修改、直接存（和 Agent 改的一样进修改记录、能撤回）。 */
async function saveFile(content: ProjectFileContent, after: string, previous: PendingFileSave | null, onCreated: (pending: PendingFileSave) => void) {
  const created = previous ? verifyManualSave(await getChangeSet(previous.changeSetId), content, previous.afterSha256) : await createProjectChangeSet(content.project_id, {
    base_revision: content.snapshot_revision,
    base_fingerprint: content.project_fingerprint,
    title: A.files.saveTitle(baseName(content.file.relative_path)),
    summary: A.files.saveSummary,
    origin: 'manual',
    gate_required: false,
    changes: [{
      operation: 'update',
      file_id: content.file.file_id,
      after_content: after,
      rationale: A.files.saveSummary,
    }],
  })
  if (!previous) onCreated({ changeSetId: created.change_set_id, text: after, afterSha256: created.changes[0].proposed_after_sha256! })
  if (created.status === 'applied') return created
  if (previous && !['reviewing', 'ready'].includes(created.status)) {
    throw new Error('原保存记录已变更，无法继续保存。请核对修改记录；当前草稿仍保留。')
  }
  try {
    return await applyChangeSetDirect(created.change_set_id, created.review_revision)
  } catch (error) {
    try {
      const recovered = await recoverManualSave(created, content)
      if (recovered.status === 'applied') return recovered
      throw new Error('原保存记录尚未确认完成，当前草稿仍保留。请稍后重新核对。')
    } catch (recoveryError) {
      throw new Error(`${authorFacingError(error, '保存结果未确认')} ${authorFacingError(recoveryError, '原修改记录暂未读回，草稿仍保留。')}`, { cause: error })
    }
  }
}

export function FileView({ projectId, file, focus, initialDraft, draftProblem, onDraft, onBack, onOpenFile, onSaved, onQuote, notify }: {
  projectId: string
  file: ProjectFile
  focus?: FileFocus
  initialDraft?: FileDraft
  draftProblem: string | null
  onDraft: (draft: FileDraft | null, submitted?: FileDraft) => void
  onBack: () => void
  onOpenFile: (path: string) => void
  onSaved: () => void
  onQuote?: (quote: FileQuotation) => void
  notify: Notify
}) {
  const [state, setState] = useState<'loading' | 'ready' | 'error'>(initialDraft ? 'ready' : 'loading')
  const [content, setContent] = useState<ProjectFileContent | null>(initialDraft?.base ?? null)
  const [editing, setEditing] = useState(Boolean(initialDraft))
  const [draft, setDraft] = useState(initialDraft?.text ?? '')
  const [saving, setSaving] = useState(false)
  const [readError, setReadError] = useState<string | null>(null)
  const [saveError, setSaveError] = useState<string | null>(null)
  const [pendingSave, setPendingSave] = useState<PendingFileSave | null>(initialDraft?.pendingSave ?? null)
  const [copied, setCopied] = useState(false)
  const [wrapText, setWrapText] = useState(() => /\.(txt|log|rst)$/i.test(file.relative_path))
  const [source, setSource] = useState(Boolean(focus?.location))
  const editorRef = useRef<HTMLTextAreaElement | null>(null)
  const headerRef = useRef<HTMLDivElement | null>(null)
  const textRef = useRef<HTMLElement | null>(null)
  const [selection, setSelection] = useState<{ text: string; location?: FileQuotation['location']; version: string; mode: string } | null>(null)
  const loadController = useRef<AbortController | null>(null)
  const visibleText = editing ? draft : content?.content ?? ''
  const lineCount = useMemo(() => countLines(visibleText), [visibleText])
  const dirty = editing && content !== null && draft !== content.content
  const externalChange = dirty && (content.file.file_id !== file.file_id || content.file.sha256 !== file.sha256)
  const mode = content?.extraction ? 'preview' : editing ? 'editor' : isMarkdown(file.relative_path) && !source ? 'preview' : 'original'
  const selected = selection?.version === visibleText && selection.mode === mode ? selection : null
  const captureSelection = useCallback(() => {
    if (headerRef.current?.contains(document.activeElement)) return
    const editor = editorRef.current
    if (editing && editor && document.activeElement === editor) {
      const { selectionStart: start, selectionEnd: end, value } = editor
      setSelection(end > start ? { text: value.slice(start, end), location: selectionLocation(value, start, end), version: visibleText, mode } : null)
      return
    }
    const current = window.getSelection()
    const node = textRef.current
    if (!editing && node && current && !current.isCollapsed && node.contains(current.anchorNode) && node.contains(current.focusNode)) {
      const range = current.getRangeAt(0)
      const before = range.cloneRange()
      before.selectNodeContents(node)
      before.setEnd(range.startContainer, range.startOffset)
      const start = before.toString().length
      const fragment = range.cloneContents()
      fragment.querySelectorAll('[data-quotation-exclude]').forEach((element) => element.remove())
      const text = fragment.textContent ?? ''
      setSelection(text ? { text, version: visibleText, mode,
        location: mode === 'original' ? selectionLocation(node.textContent ?? '', start, start + text.length) : undefined } : null)
      return
    }
    setSelection(null)
  }, [editing, mode, visibleText])
  useEffect(() => {
    document.addEventListener('selectionchange', captureSelection)
    return () => document.removeEventListener('selectionchange', captureSelection)
  }, [captureSelection])
  const quote = () => {
    if (!onQuote || state !== 'ready' || saving) return
    onQuote({ projectId, path: file.relative_path,
      ...(selected ? { text: selected.text, location: selected.location, source: dirty ? 'draft' : mode === 'preview' ? 'preview' : 'original' }
        : dirty ? { text: visibleText, source: 'draft' } : {}),
    })
    setSelection(null)
  }

  useLayoutEffect(() => {
    if (focus?.location) setSource(true)
  }, [focus])
  useLayoutEffect(() => {
    if (editing && state === 'ready' && focus?.location && editorRef.current) focusFileEditor(editorRef.current, focus.location)
  }, [editing, state, focus])

  useEffect(() => { setCopied(false) }, [visibleText])
  useEffect(() => {
    if (!copied) return undefined
    const timer = setTimeout(() => setCopied(false), 2_000)
    return () => clearTimeout(timer)
  }, [copied])

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(visibleText)
      setCopied(true)
    } catch {
      notify('danger', '复制没有成功，请选中文字后复制。')
    }
  }

  const load = useCallback(async () => {
    loadController.current?.abort()
    const controller = new AbortController()
    loadController.current = controller
    const signal = controller.signal
    setState('loading')
    setReadError(null)
    try {
      const next = await readBoundProjectFile(projectId, file.file_id, { signal })
      if (signal?.aborted) return
      setContent(next)
      setState('ready')
    } catch (error) {
      if (signal?.aborted) return
      setState('error')
      setReadError(authorFacingError(error, A.files.readFailed))
    }
  }, [file.file_id, projectId])

  // 文件换了、或者被改过（sha256 变了）就重读；正在改的时候不打断。
  useEffect(() => {
    if (editing) return undefined
    void load()
    return () => loadController.current?.abort()
  }, [editing, file.sha256, load])

  const save = async () => {
    if (!content || saving || content.extraction) return
    if (draft === content.content && !pendingSave) {
      onDraft(null, { base: content, text: draft })
      setEditing(false)
      return
    }
    setSaving(true)
    setSaveError(null)
    try {
      const submitted = { base: content, text: draft }
      const savingText = pendingSave?.text ?? draft
      const saved = await saveFile(content, savingText, pendingSave, (pending) => {
        setPendingSave(pending)
        onDraft({ ...submitted, pendingSave: pending }, submitted)
      })
      if (savingText !== draft) {
        // Confirm the old save first, then rebase the newer draft on its actual
        // disk version. A failed read retains the original receipt for retry.
        const fileId = saved.writeback?.files[0]?.result_file_id ?? content.file.file_id
        const next = await readBoundProjectFile(projectId, fileId)
        if (next.file.sha256 !== saved.changes[0].proposed_after_sha256) {
          throw new Error('上次保存已完成，但文件后来又有变化。当前草稿仍保留，请先核对最新文件。')
        }
        setContent(next)
        setPendingSave(null)
        onDraft({ base: next, text: draft }, submitted)
        onSaved()
        notify('ok', '上次保存已确认，后来编辑的内容仍是草稿，可以继续保存。')
        return
      }
      setPendingSave(null)
      onDraft(null, { base: content, text: draft })
      notify('ok', A.files.saved(baseName(file.relative_path)))
      setEditing(false)
      onSaved()
    } catch (error) {
      const message = authorFacingError(error, A.files.saveFailed)
      setSaveError(message)
      notify('danger', message)
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="zm-file">
      <div className="zm-file-head is-file-view" ref={headerRef}>
        <div className="zm-file-toolbar">
          <IconBtn size="sm" label={A.files.back} icon={<ArrowLeft />} onClick={onBack} disabled={saving} />
          <div className="zm-file-identity">
            <div className="zm-file-name"><FileText aria-hidden /><h3 title={file.relative_path}>{baseName(file.relative_path)}</h3></div>
          </div>
          <div className="zm-file-actions">
            {state === 'ready' && content ? (
              <>
                {onQuote ? <IconBtn size="sm" icon={<MessageSquarePlus />} label={selected ? '引用选中内容' : dirty ? '引用当前草稿' : '引用文件到对话'}
                  disabled={saving} onMouseDown={(event) => event.preventDefault()} onClick={quote} /> : null}
                <IconBtn size="sm" icon={copied ? <Check /> : <Copy />}
                  label={copied ? '已复制文件内容' : editing ? '复制当前内容' : '复制文件内容'}
                  onClick={() => void copy()} />
                {editing ? (
                  <>
                    <Btn size="sm" variant="ghost" onClick={() => { onDraft(null, { base: content, text: draft }); setPendingSave(null); setSaveError(null); setEditing(false) }} disabled={saving}>{A.files.cancel}</Btn>
                    <Btn size="sm" variant="primary" busy={saving} onClick={() => void save()}>{A.files.save}</Btn>
                  </>
                ) : !content.extraction ? (
                  <Btn size="sm" onClick={() => { setDraft(content.content); setEditing(true) }}>{A.files.edit}</Btn>
                ) : null}
              </>
            ) : null}
          </div>
        </div>
          {file.relative_path !== baseName(file.relative_path) ? <div className="zm-file-path" title={file.relative_path}><bdi>{file.relative_path}</bdi></div> : null}
          {state === 'ready' && content ? <div className="zm-file-meta">
            <span className={`zm-file-state${dirty ? ' is-dirty' : ''}`} role="status">{saving ? '正在保存…' : pendingSave ? '保存待确认' : dirty ? '未保存' : editing ? '编辑中' : content.extraction ? '文字提取 · 只读' : '预览'}</span>
            <span>{lineCount} 行</span>
            {isMarkdown(file.relative_path) && !editing ? <div className="zm-segment" role="group" aria-label="文件显示方式">
              <button type="button" aria-pressed={!source} className={!source ? 'is-on' : ''} onClick={() => setSource(false)}>预览</button>
              <button type="button" aria-pressed={source} className={source ? 'is-on' : ''} onClick={() => setSource(true)}>原文</button>
            </div> : null}
            {!isMarkdown(file.relative_path) || source ? <button type="button" className="zm-file-wrap" aria-pressed={wrapText}
              title={wrapText ? '自动换行已开启，关闭后显示原始行号' : '长行横向滚动，开启后按面板宽度换行'}
              onClick={() => setWrapText((current) => !current)}>自动换行</button> : null}
          </div> : null}
          {selected && onQuote ? <div className="zm-file-selection" role="group" aria-label="文件选段">
            <Quote aria-hidden /><span>{dirty ? '草稿选段' : mode === 'preview' ? '预览选段' : '原文选段'} · {selected.text.length.toLocaleString('zh-CN')} 字符</span>
            <Btn size="sm" variant="ghost" icon={<MessageSquarePlus />} disabled={saving} onMouseDown={(event) => event.preventDefault()} onClick={quote}>加入对话</Btn>
          </div> : null}
      </div>
      <div className="zm-file-body">
        {state === 'ready' && content?.extraction ? <div className="zm-file-extraction" role="note" aria-label="文档提取范围">
          <p>{content.extraction.format.toUpperCase()} 文字预览 · 可复制或引用到对话</p>
          {content.extraction.warnings.map((warning) => <p key={warning}>{warning}</p>)}
        </div> : null}
        {state === 'loading' ? <p className="zm-muted">{A.files.loading}</p> : null}
        {state === 'error' ? <div>
          {/file is not readable text|file contains binary data/i.test(readError ?? '') ? (
            <div className="zm-error-line" role="alert">
              <p>这个文件暂时无法按文字预览，可能是二进制文件或尚未支持的文字编码。</p>
              <details><summary>查看原始错误</summary><p>{readError}</p></details>
            </div>
          ) : <p className="zm-error-line" role="alert">{readError ?? A.files.readFailed}</p>}
          <Btn size="sm" onClick={() => void load()}>重新读取</Btn>
        </div> : null}
        {saveError ? <p className="zm-error-line" role="alert">{saveError}；当前输入已保留。</p> : null}
        {draftProblem ? <p className="zm-error-line" role="alert">{draftProblem}</p> : null}
        {state === 'ready' && focus?.location && !fileTextSelection(visibleText, focus.location) ? <p className="zm-muted" role="status">引用的第 {focus.location.line} 行{focus.location.column ? `第 ${focus.location.column} 列` : ''}已超出当前文件，内容可能已变化。</p> : null}
        {externalChange ? <p className="zm-muted">文件已有外部修改，当前保留的是你的草稿；保存时会核对原版本。可以先复制草稿，再重新读取文件。</p> : null}
        {state === 'ready' && content ? (
          editing ? (
            <textarea
              ref={editorRef}
              className={`zm-file-editor${isMarkdown(file.relative_path) ? '' : ' is-code'}`}
              spellCheck={false}
              wrap={(isMarkdown(file.relative_path) && !source) || wrapText ? 'soft' : 'off'}
              value={draft}
              aria-label={A.files.editorLabel(file.relative_path)}
              disabled={saving}
              onSelect={captureSelection}
              onChange={(event) => {
                const text = event.target.value
                setDraft(text)
                if (text === content.content && !pendingSave) onDraft(null, { base: content, text: draft })
                else onDraft({ base: content, text, ...(pendingSave ? { pendingSave } : {}) })
              }}
              autoFocus
            />
          ) : isMarkdown(file.relative_path) && !source ? (
            <div className="zm-file-prose" ref={(node) => { textRef.current = node }}><Markdown text={content.content} onOpenFile={onOpenFile} /></div>
          ) : (
            <TextPreview text={content.content} path={file.relative_path} wrap={wrapText} focus={focus} textRef={textRef} />
          )
        ) : null}
      </div>
    </div>
  )
}

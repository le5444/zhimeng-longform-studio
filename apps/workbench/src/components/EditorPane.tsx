import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { EditorContent, useEditor } from '@tiptap/react'
import Placeholder from '@tiptap/extension-placeholder'
import StarterKit from '@tiptap/starter-kit'
import {
  AlertCircle,
  Bold,
  Check,
  ChevronDown,
  Columns2,
  Heading2,
  Italic,
  LockKeyhole,
  LoaderCircle,
  MessageSquareText,
  MoreHorizontal,
  Quote,
  RefreshCw,
  Redo2,
  Save,
  Undo2,
  Wand2,
} from 'lucide-react'
import type { AgentCandidateOutput } from '../api/agent'
import type { CandidateFileChange, ChangeSet, HunkDecision } from '../api/changeSets'
import { candidateExcerpt, currentDraft } from '../data/workbenchFixture'
import {
  editorDocumentToPlainText,
  plainTextToEditorHtml,
} from '../lib/proseDocument'
import { SafeMarkdown } from './SafeMarkdown'

type DocumentMode = 'draft' | 'candidate' | 'diff' | 'reader'

const documentModes: Array<{ id: DocumentMode; label: string }> = [
  { id: 'draft', label: '正文' },
  { id: 'candidate', label: '助手结果' },
  { id: 'diff', label: '修改对比' },
  { id: 'reader', label: '审稿预览' },
]

interface IconButtonProps {
  label: string
  active?: boolean
  disabled?: boolean
  onClick: () => void
  children: React.ReactNode
}

function IconButton({ label, active, disabled, onClick, children }: IconButtonProps) {
  return (
    <button
      className={`icon-button${active ? ' is-active' : ''}`}
      type="button"
      aria-label={label}
      title={label}
      aria-pressed={active}
      disabled={disabled}
      onClick={onClick}
    >
      {children}
    </button>
  )
}

interface EditorPaneProps {
  content?: string | null
  readOnly?: boolean
  demo?: boolean
  fileId?: string | null
  relativePath?: string | null
  candidateOutput?: AgentCandidateOutput | null
  changeSet?: ChangeSet | null
  viewRequest?: {
    mode: Exclude<DocumentMode, 'reader'>
    key: number
    changeSetId?: string | null
  } | null
  onReviewChangeSet?: (changeSetId: string) => void
  onReviewCandidateEvidence?: () => void
  onSave?: (plainText: string) => void | Promise<void>
  /** 作者选中一段并给出要求后调用；返回 Promise 便于显示进度。 */
  onRewritePassage?: (passage: string, instruction: string) => Promise<void>
  saveState?: 'idle' | 'saving' | 'error'
  saveError?: string | null
  status?: {
    kind: 'loading' | 'error'
    message: string
    onRetry?: () => void
  } | null
}

const candidateKindLabels: Record<AgentCandidateOutput['kind'], string> = {
  repair_plan: '修复方案',
  risk_draft: '风险草稿',
  change_proposal: '修改建议',
  next_step_advice: '下一步建议',
}

const hunkDecisionLabels: Record<HunkDecision, string> = {
  pending: '待决定',
  accepted: '已接受',
  rejected: '已拒绝',
}

function matchesActiveFile(
  change: CandidateFileChange,
  fileId: string | null,
  relativePath: string | null,
): boolean {
  if (fileId && change.file_id === fileId) return true
  return Boolean(relativePath && change.relative_path === relativePath)
}

export function EditorPane({
  content = null,
  readOnly = false,
  demo = false,
  fileId = null,
  relativePath = null,
  candidateOutput = null,
  changeSet = null,
  viewRequest = null,
  onReviewChangeSet,
  onReviewCandidateEvidence,
  onSave,
  onRewritePassage,
  saveState = 'idle',
  saveError = null,
  status = null,
}: EditorPaneProps) {
  const [documentMode, setDocumentMode] = useState<DocumentMode>('draft')
  const [characterCount, setCharacterCount] = useState(2678)
  const [dirty, setDirty] = useState(false)
  const [, setEditorRevision] = useState(0)
  const handledViewRequestKey = useRef<number | null>(null)
  const baselinePlainRef = useRef<string | null>(null)
  const editor = useEditor({
    extensions: [
      StarterKit,
      Placeholder.configure({ placeholder: '从这一章继续写…' }),
    ],
    content: currentDraft,
    editorProps: {
      attributes: {
        class: 'prose-editor',
        spellcheck: 'false',
        'aria-label': '章节正文编辑器',
      },
    },
    onSelectionUpdate: () => setEditorRevision((value) => value + 1),
    onUpdate: ({ editor: currentEditor }) => {
      const plain = editorDocumentToPlainText(currentEditor.getJSON())
      setCharacterCount(currentEditor.getText().replace(/\s/g, '').length)
      setEditorRevision((value) => value + 1)
      if (baselinePlainRef.current !== null) {
        setDirty(plain !== baselinePlainRef.current)
      }
    },
  })

  const fileChanges = useMemo(
    () => changeSet?.changes.filter((change) => (
      matchesActiveFile(change, fileId, relativePath)
    )) ?? [],
    [changeSet, fileId, relativePath],
  )
  const hasCandidate = demo || candidateOutput !== null || fileChanges.length > 0
  const hasDiff = demo || fileChanges.length > 0
  const modeAvailable = useCallback((mode: DocumentMode) => (
    mode === 'draft'
    || (mode === 'candidate' && hasCandidate)
    || (mode === 'diff' && hasDiff)
    || (mode === 'reader' && demo)
  ), [demo, hasCandidate, hasDiff])
  const changeSetStatus = changeSet?.status
  const diffHunkCount = fileChanges.reduce(
    (total, change) => total + change.hunks.length,
    0,
  )

  useEffect(() => {
    if (!editor) return
    if (content === null) {
      editor.commands.setContent(currentDraft, { emitUpdate: false })
      baselinePlainRef.current = editorDocumentToPlainText(editor.getJSON())
    } else {
      editor.commands.setContent(plainTextToEditorHtml(content), { emitUpdate: false })
      baselinePlainRef.current = editorDocumentToPlainText(editor.getJSON())
    }
    setDirty(false)
    setCharacterCount(editor.getText().replace(/\s/g, '').length)
  }, [content, editor])

  const saving = saveState === 'saving'
  // 选中一段才能重写。TipTap 的选区变化要订阅，不能只在渲染时读。
  const [selectedText, setSelectedText] = useState('')
  const [rewriteOpen, setRewriteOpen] = useState(false)
  const [rewriteInstruction, setRewriteInstruction] = useState('')
  const [rewriting, setRewriting] = useState(false)
  const [rewriteError, setRewriteError] = useState<string | null>(null)

  useEffect(() => {
    if (!editor) return
    const sync = () => {
      const { from, to } = editor.state.selection
      // 段落之间用空行分隔，跟磁盘上的正文格式保持一致，
      // 否则后端拿选中文本去文件里比对会对不上。
      setSelectedText(
        from === to ? '' : editor.state.doc.textBetween(from, to, '\n\n'),
      )
    }
    editor.on('selectionUpdate', sync)
    editor.on('transaction', sync)
    return () => {
      editor.off('selectionUpdate', sync)
      editor.off('transaction', sync)
    }
  }, [editor])

  const submitRewrite = useCallback(async () => {
    if (!onRewritePassage || !selectedText.trim() || !rewriteInstruction.trim()) return
    setRewriting(true)
    setRewriteError(null)
    try {
      await onRewritePassage(selectedText, rewriteInstruction.trim())
      setRewriteOpen(false)
      setRewriteInstruction('')
    } catch (caught) {
      setRewriteError(caught instanceof Error ? caught.message : '重写失败')
    } finally {
      setRewriting(false)
    }
  }, [onRewritePassage, selectedText, rewriteInstruction])

  const canPersist = Boolean(onSave) && !readOnly && documentMode === 'draft' && dirty && !saving

  const pendingSavePlainRef = useRef<string | null>(null)

  const handleSave = useCallback(() => {
    if (!editor || !canPersist || !onSave) return
    const plain = editorDocumentToPlainText(editor.getJSON())
    pendingSavePlainRef.current = plain
    void onSave(plain)
  }, [canPersist, editor, onSave])

  useEffect(() => {
    if (saveState === 'error') {
      pendingSavePlainRef.current = null
      return
    }
    if (saveState !== 'idle' || saveError || !editor || pendingSavePlainRef.current === null) return
    const pending = pendingSavePlainRef.current
    pendingSavePlainRef.current = null
    const current = editorDocumentToPlainText(editor.getJSON())
    if (current === pending) {
      baselinePlainRef.current = pending
      setDirty(false)
    }
  }, [editor, saveError, saveState])

  useEffect(() => {
    editor?.setEditable(!readOnly && documentMode === 'draft')
  }, [documentMode, editor, readOnly])

  useEffect(() => {
    if (!modeAvailable(documentMode)) setDocumentMode('draft')
  }, [documentMode, modeAvailable])

  useEffect(() => {
    if (!viewRequest || handledViewRequestKey.current === viewRequest.key) return
    if (
      viewRequest.mode === 'diff'
      && (
        !viewRequest.changeSetId
        || changeSet?.change_set_id !== viewRequest.changeSetId
      )
    ) return
    if (!modeAvailable(viewRequest.mode)) return
    handledViewRequestKey.current = viewRequest.key
    setDocumentMode(viewRequest.mode)
  }, [changeSet?.change_set_id, modeAvailable, viewRequest])

  useEffect(() => {
    if (changeSetStatus && ['applied', 'rolled_back'].includes(changeSetStatus)) {
      setDocumentMode('draft')
    }
  }, [changeSetStatus])

  return (
    <main className="editor-pane panel-mobile" data-mobile-pane="editor">
      <header className="document-header">
        <div className="document-tabs" role="tablist" aria-label="文稿视图">
          {documentModes.map((mode) => (
            <button
              type="button"
              role="tab"
              aria-selected={documentMode === mode.id}
              className={documentMode === mode.id ? 'is-active' : ''}
              key={mode.id}
              disabled={!modeAvailable(mode.id)}
              onClick={() => setDocumentMode(mode.id)}
            >
              {mode.label}
              {mode.id === 'candidate' && hasCandidate && <span className="count-mark">1</span>}
              {mode.id === 'diff' && hasDiff && <span className="count-mark">{demo ? 3 : diffHunkCount}</span>}
            </button>
          ))}
        </div>
        <div className="document-actions">
          <span className={`save-state${dirty ? ' is-dirty' : ''}${saveState === 'error' ? ' is-error' : ''}`}>
            {readOnly
              ? <><LockKeyhole size={13} />只读快照</>
              : saving
                ? <><LoaderCircle className="spin" size={13} />正在写回</>
                : saveState === 'error'
                  ? <><AlertCircle size={13} />写回失败</>
                  : dirty
                    ? <><AlertCircle size={13} />未保存</>
                    : <><Check size={13} />已保存</>}
          </span>
          <IconButton
            label={saving ? '正在写回' : '保存正文'}
            disabled={!canPersist}
            onClick={handleSave}
          >
            <Save size={16} />
          </IconButton>
          <IconButton label="更多文稿操作" onClick={() => undefined}><MoreHorizontal size={17} /></IconButton>
        </div>
      </header>

      <div className="editor-toolbar" aria-label="正文格式工具栏">
        <IconButton label="撤销" disabled={readOnly || !editor?.can().undo()} onClick={() => editor?.chain().focus().undo().run()}><Undo2 size={15} /></IconButton>
        <IconButton label="重做" disabled={readOnly || !editor?.can().redo()} onClick={() => editor?.chain().focus().redo().run()}><Redo2 size={15} /></IconButton>
        <span className="toolbar-divider" />
        <IconButton label="粗体" disabled={readOnly} active={editor?.isActive('bold')} onClick={() => editor?.chain().focus().toggleBold().run()}><Bold size={15} /></IconButton>
        <IconButton label="斜体" disabled={readOnly} active={editor?.isActive('italic')} onClick={() => editor?.chain().focus().toggleItalic().run()}><Italic size={15} /></IconButton>
        <IconButton label="二级标题" disabled={readOnly} active={editor?.isActive('heading', { level: 2 })} onClick={() => editor?.chain().focus().toggleHeading({ level: 2 }).run()}><Heading2 size={15} /></IconButton>
        <IconButton label="引用" disabled={readOnly} active={editor?.isActive('blockquote')} onClick={() => editor?.chain().focus().toggleBlockquote().run()}><Quote size={15} /></IconButton>
        <span className="toolbar-divider" />
        <button type="button" className="toolbar-select" title="正文样式" disabled={readOnly}>正文 <ChevronDown size={13} /></button>
        {onRewritePassage && (
          <>
            <span className="toolbar-divider" />
            <button
              type="button"
              className="toolbar-rewrite"
              title={selectedText ? '让模型按你的要求重写选中的这一段' : '先在正文里选中一段'}
              disabled={!selectedText.trim() || rewriting}
              onClick={() => { setRewriteError(null); setRewriteOpen((v) => !v) }}
            >
              <Wand2 size={14} />
              重写这段{selectedText.trim() ? `（${selectedText.trim().length} 字）` : ''}
            </button>
          </>
        )}
        <div className="toolbar-spacer" />
        <span className="editor-count">{characterCount} 字</span>
      </div>

      {rewriteOpen && onRewritePassage && (
        <div className="rewrite-bar">
          <input
            className="rewrite-input"
            type="text"
            value={rewriteInstruction}
            placeholder="想怎么改？例如：多用短句、删掉解释的话、把情绪写得更克制"
            onChange={(event) => setRewriteInstruction(event.target.value)}
            onKeyDown={(event) => { if (event.key === 'Enter') void submitRewrite() }}
            disabled={rewriting}
            autoFocus
          />
          <button
            type="button"
            className="rewrite-submit"
            onClick={() => void submitRewrite()}
            disabled={rewriting || !rewriteInstruction.trim()}
          >
            {rewriting ? '正在改…' : '开始重写'}
          </button>
          <button
            type="button"
            className="rewrite-cancel"
            onClick={() => { setRewriteOpen(false); setRewriteError(null) }}
            disabled={rewriting}
          >
            取消
          </button>
          {rewriteError !== null && <p className="rewrite-error" role="alert">{rewriteError}</p>}
        </div>
      )}

      <section className="editor-stage">
        {saveError && !status && (
          <div className="editor-state editor-state-error" role="alert">
            <AlertCircle size={20} />
            <strong>正文写回失败</strong>
            <span>{saveError}</span>
          </div>
        )}
        {status && (
          <div className={`editor-state editor-state-${status.kind}`} role={status.kind === 'error' ? 'alert' : 'status'}>
            {status.kind === 'loading' ? <LoaderCircle className="spin" size={20} /> : <AlertCircle size={20} />}
            <strong>{status.kind === 'loading' ? '正在读取章节' : '章节不可用'}</strong>
            <span>{status.message}</span>
            {status.kind === 'error' && status.onRetry && <button type="button" className="quiet-button" onClick={status.onRetry}><RefreshCw size={14} />重试</button>}
          </div>
        )}
        {!status && documentMode === 'draft' && (
          <div className="paper-surface">
            <EditorContent editor={editor} />
          </div>
        )}

        {!status && documentMode === 'candidate' && demo && (
          <div className="candidate-surface">
            <div className="surface-heading">
              <div><span className="eyebrow">候选 01</span><h2>压缩入场，提前显露威胁</h2></div>
              <div className="inline-actions"><button type="button" className="quiet-button"><Columns2 size={15} />对比</button><button type="button" className="primary-button"><Check size={15} />采用</button></div>
            </div>
            {candidateExcerpt.map((paragraph) => <p key={paragraph}>{paragraph}</p>)}
            <div className="candidate-note"><MessageSquareText size={15} /><span>保留黄铜钥匙与葬礼来客两条线索，删去 126 字解释。</span></div>
          </div>
        )}

        {!status && documentMode === 'candidate' && !demo && (
          <div className="candidate-surface candidate-surface-live">
            <div className="surface-heading">
              <div>
                <span className="eyebrow">{candidateOutput ? candidateKindLabels[candidateOutput.kind] : 'ChangeSet 候选'}</span>
                <h2>{candidateOutput?.content.title ?? changeSet?.title ?? '写作助手结果'}</h2>
                <p className="candidate-summary">{candidateOutput?.content.summary ?? changeSet?.summary}</p>
              </div>
              <div className="inline-actions">
                {hasDiff && <button type="button" className="quiet-button" onClick={() => setDocumentMode('diff')}><Columns2 size={15} />查看修改对比</button>}
                {changeSet && onReviewChangeSet && <button type="button" className="primary-button" onClick={() => onReviewChangeSet(changeSet.change_set_id)}><Check size={15} />审阅改动</button>}
                {!changeSet && onReviewCandidateEvidence && <button type="button" className="quiet-button" onClick={onReviewCandidateEvidence}><MessageSquareText size={15} />查看运行证据</button>}
              </div>
            </div>
            {candidateOutput ? (
              <section className="live-candidate-body" aria-label="写作助手结果内容">
                <SafeMarkdown content={candidateOutput.content.body} />
              </section>
            ) : (
              <div className="candidate-empty-copy">
                <MessageSquareText size={17} />
                <div><strong>候选改动已进入审阅队列</strong><span>当前 API 只提供逐 Hunk 差异，不把差异片段冒充完整候选章节。</span></div>
              </div>
            )}
            {candidateOutput && (
              <div className="candidate-note candidate-policy-note">
                <LockKeyhole size={15} />
                <span>正式正文未授权 · formal_draft_allowed=false · 只有 ChangeSet 经作者逐项决议和 Gate 后才能写入项目。</span>
              </div>
            )}
            {changeSet && (
              <dl className="candidate-meta">
                <div><dt>ChangeSet</dt><dd>{changeSet.change_set_id}</dd></div>
                <div><dt>来源</dt><dd>{changeSet.source_run_id ? `Run ${changeSet.source_run_id}` : changeSet.origin}</dd></div>
                <div><dt>状态</dt><dd>{changeSet.status} · review rev {changeSet.review_revision}</dd></div>
              </dl>
            )}
          </div>
        )}

        {!status && documentMode === 'diff' && demo && (
          <div className="diff-surface">
            <div className="diff-toolbar"><span>第十二章 · 3 处修改</span><span className="diff-stat"><b>+84</b><i>−126</i></span></div>
            <div className="diff-columns">
              <article><h3>当前稿</h3><p className="removed">先是一层薄雾贴着旧港口的石阶，等林见川从档案馆出来，檐角的水已经连成了线。</p><p>他把那封没有署名的信塞进大衣内袋。</p></article>
              <article><h3>修改后</h3><p className="added">雨声压低了整座旧港口。林见川从档案馆出来时，檐角的水已经连成了线。</p><p>他把没有署名的信压进大衣内袋。</p></article>
            </div>
          </div>
        )}

        {!status && documentMode === 'diff' && !demo && changeSet && (
          <div className="diff-surface diff-surface-live">
            <div className="diff-toolbar">
              <span>{changeSet.title} · {diffHunkCount} 处修改</span>
              {onReviewChangeSet && <button type="button" className="quiet-button" onClick={() => onReviewChangeSet(changeSet.change_set_id)}><Check size={14} />在右侧审阅</button>}
            </div>
            {fileChanges.map((change) => (
              <section className="diff-file-block" key={change.change_id}>
                <header><strong>{change.relative_path}</strong><span>{change.rationale}</span></header>
                {change.hunks.map((hunk) => (
                  <div className="diff-hunk" key={hunk.hunk_id}>
                    <div className="diff-hunk-head">
                      <span>Hunk {hunk.index + 1} · 原稿 {hunk.before_start + 1}-{hunk.before_end} 行</span>
                      <strong className={`hunk-decision decision-${hunk.decision}`}>{hunkDecisionLabels[hunk.decision]}</strong>
                    </div>
                    <div className="diff-columns">
                      <article><h3>写回前基线</h3><pre className="removed">{hunk.before_text || '（新增内容）'}</pre></article>
                      <article><h3>候选修改</h3><pre className="added">{hunk.after_text || '（删除内容）'}</pre></article>
                    </div>
                  </div>
                ))}
              </section>
            ))}
          </div>
        )}

        {!status && documentMode === 'reader' && (
          <div className="reader-preview">
            <div className="reader-preview-head"><span className="avatar">冷</span><div><h2>冷读视图</h2><p>只使用已发布前文与当前候选</p></div></div>
            <blockquote>“黑色轿车是谁的”比“钥匙能打开什么”更让我想继续读，但结尾认出女人后还缺一个即时危险。</blockquote>
            <dl><div><dt>读者理解</dt><dd>父亲失踪与档案馆旧案开始汇合</dd></div><div><dt>当前误会</dt><dd>可能把周鹤亭当成来信人</dd></div><div><dt>章末预测</dt><dd>女人会要求林见川交出钥匙</dd></div></dl>
          </div>
        )}
      </section>
    </main>
  )
}

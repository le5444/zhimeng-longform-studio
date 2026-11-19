import { useEffect, useRef, useState } from 'react'
import { ArrowLeft, Check, Copy, FileText, FolderOpen, MessageSquarePlus, RotateCw } from 'lucide-react'
import { readLocalFile, revealLocalFile, type LocalFilePreview } from '../../api/fs'
import { authorFacingError } from '../../lib/authorFacingError'
import { Markdown } from '../chat/Markdown'
import { fileLocation, type FileLocation } from '../lib/fileReferences'
import { fileTextSelection, type FileFocus } from '../lib/filePosition'
import { localFileCandidates } from '../lib/fileNavigation'
import type { FileQuotation } from '../lib/fileQuotation'
import { Btn, IconBtn, type Notify } from '../ui'
import { TextPreview } from './FileView'

export interface LocalFileTarget {
  sessionId: string | null
  projectId: string | null
  path: string
  location?: FileLocation
  request: number
  returnPath: string | null
}

export function LocalFilePanel(props: { target: LocalFileTarget; refreshKey: string; onOpenFile: (path: string) => void;
  onBack: () => void; onQuote?: (quote: FileQuotation) => void; notify: Notify }) {
  return <LocalFileContent key={`${props.target.sessionId}:${props.target.path}:${props.target.request}`} {...props} />
}

function LocalFileContent({ target, refreshKey, onOpenFile, onBack, onQuote, notify }: Parameters<typeof LocalFilePanel>[0]) {
  const [content, setContent] = useState<LocalFilePreview | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [reload, setReload] = useState(0)
  const [source, setSource] = useState(Boolean(target.location ?? fileLocation(target.path)))
  const [wrap, setWrap] = useState(false)
  const [copied, setCopied] = useState(false)
  const textRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    const controller = new AbortController()
    setBusy(true); setError(null); setContent(null)
    void readLocalFile(localFileCandidates(target.path), { signal: controller.signal }).then(next => {
      if (!controller.signal.aborted) setContent(next)
    }, cause => {
      if (!controller.signal.aborted) setError(authorFacingError(cause, '文件暂时无法读取。'))
    }).finally(() => { if (!controller.signal.aborted) setBusy(false) })
    return () => controller.abort()
  }, [target.path, refreshKey, reload])
  useEffect(() => { setCopied(false) }, [content])
  useEffect(() => {
    if (!copied) return
    const timer = window.setTimeout(() => setCopied(false), 2000)
    return () => window.clearTimeout(timer)
  }, [copied])
  const path = content?.path ?? localFileCandidates(target.path)[0] ?? target.path
  const name = path.split(/[\\/]/).at(-1) ?? path
  const text = content?.content ?? ''
  const markdown = /\.(md|markdown)$/i.test(path) && content?.kind === 'text'
  // An existing literal #L filename wins over a location suffix.
  const location = target.location ?? (content && path.replace(/\\/g, '/') !== localFileCandidates(target.path)[0]?.replace(/\\/g, '/') ? fileLocation(target.path) : undefined)
  const focus: FileFocus = { location, request: target.request }
  const openFile = (href: string) => {
    if (localFileCandidates(href).length) onOpenFile(href)
    else {
      const parent = path.replace(/\\/g, '/').split('/').slice(0, -1).join('/')
      onOpenFile(`${parent}/${href}`)
    }
  }
  const copy = async () => {
    try { await navigator.clipboard.writeText(text); setCopied(true) }
    catch (cause) { notify('warn', authorFacingError(cause, '复制没有成功，请选中文字复制。')) }
  }
  const quote = () => {
    if (!content || content.content === null) return
    const selection = window.getSelection(), region = textRef.current
    const selected = selection && !selection.isCollapsed && region?.contains(selection.anchorNode) && region.contains(selection.focusNode)
      ? selection.toString() : undefined
    onQuote?.({ projectId: target.projectId, path,
      ...(selected ? { text: selected, source: 'preview' as const } : {}) })
  }
  const reveal = async () => {
    try { await revealLocalFile(path) }
    catch (cause) { notify('warn', authorFacingError(cause, '暂时无法打开所在文件夹。')) }
  }
  return <div className="zm-file">
    <div className="zm-file-head is-file-view">
      <div className="zm-file-toolbar">
        <div className="zm-file-identity"><div className="zm-file-name"><FileText aria-hidden /><h3 title={path}>{name}</h3></div></div>
        <div className="zm-file-actions">
          <IconBtn size="sm" icon={<ArrowLeft />} label="返回文件" onClick={onBack} />
          {content?.content !== null && content?.content !== undefined ? <>
            {onQuote ? <IconBtn size="sm" icon={<MessageSquarePlus />} label="引用文件到对话" onMouseDown={event => event.preventDefault()} onClick={quote} /> : null}
            <IconBtn size="sm" icon={copied ? <Check /> : <Copy />} label={copied ? '已复制文件内容' : '复制文件内容'} onClick={() => void copy()} />
          </> : null}
          <IconBtn size="sm" icon={<FolderOpen />} label="打开所在文件夹" onClick={() => void reveal()} />
          <IconBtn size="sm" icon={<RotateCw />} label="重新读取文件" disabled={busy} onClick={() => setReload(value => value + 1)} />
        </div>
      </div>
      <div className="zm-file-path" title={path}><bdi>{path}</bdi></div>
      {content?.content !== null && content?.content !== undefined ? <div className="zm-file-meta">
        <span className="zm-file-state" role="status">{content.kind === 'office' ? '文字提取 · 只读' : '预览'}</span>
        {markdown ? <div className="zm-segment" role="group" aria-label="文件显示方式">
          <button type="button" aria-pressed={!source} onClick={() => setSource(false)}>预览</button>
          <button type="button" aria-pressed={source} onClick={() => setSource(true)}>原文</button>
        </div> : null}
        {!markdown || source ? <button type="button" className="zm-file-wrap" aria-pressed={wrap} onClick={() => setWrap(value => !value)}>自动换行</button> : null}
      </div> : null}
    </div>
    <div className="zm-file-body">
      {busy ? <p role="status">正在读取文件…</p> : null}
      {error ? <div><p role="alert" className="zm-error-line">{error}</p><Btn size="sm" onClick={() => setReload(value => value + 1)}>重新读取</Btn></div> : null}
      {content?.warnings.length ? <div className="zm-file-extraction" role="note">{content.warnings.map(warning => <p key={warning}>{warning}</p>)}</div> : null}
      {content?.kind === 'directory' ? <p>这是文件夹，可以打开所在位置查看文件。</p> : null}
      {content?.content !== null && content?.content !== undefined ? <>
        {location && !fileTextSelection(text, location) ? <p role="status">引用的第 {location.line} 行已超出当前内容。</p> : null}
        {markdown && !source ? <div className="zm-file-prose" ref={node => { textRef.current = node }}><Markdown text={text} onOpenFile={openFile} /></div>
          : <TextPreview text={text} path={path} wrap={wrap} focus={focus} textRef={textRef} />}
      </> : null}
    </div>
  </div>
}

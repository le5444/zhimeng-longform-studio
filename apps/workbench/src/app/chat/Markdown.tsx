import { isValidElement, memo, useCallback, useLayoutEffect, useMemo, useRef, useState, type ComponentPropsWithoutRef } from 'react'
import { Check, Copy, FileText } from 'lucide-react'
import ReactMarkdown, { defaultUrlTransform } from 'react-markdown'
import remarkGfm from 'remark-gfm'
import { T } from '../strings'
import { decodeFileHref, isLocalFileHref } from '../lib/fileReferences'
import { remarkFileMentions } from './remarkFileMentions'

function CodeBlock({ children, node: _node, ...props }: ComponentPropsWithoutRef<'pre'> & { node?: unknown }) {
  const [copied, setCopied] = useState(false)
  const code = isValidElement(children) ? children : null
  const className = (code?.props as { className?: string } | undefined)?.className ?? ''
  const lang = /language-([\w+-]+)/.exec(className)?.[1] ?? ''
  const raw = code ? String((code.props as { children?: unknown }).children ?? '') : String(children ?? '')
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(raw.replace(/\n$/, ''))
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1600)
    } catch {
      // 剪贴板不让用：什么也不做，代码就在眼前
    }
  }
  return (
    <div className="zm-code">
      <div className="zm-code-head" data-quotation-exclude>
        <span>{lang || T.agent.codeBlock.fallbackLang}</span>
        <button type="button" className="zm-code-copy" onClick={() => void copy()} aria-label={copied ? T.agent.codeBlock.copied : T.agent.codeBlock.copy}>
          {copied ? <Check aria-hidden /> : <Copy aria-hidden />}
          <span>{copied ? T.agent.codeBlock.copied : T.agent.codeBlock.copy}</span>
        </button>
      </div>
      <pre {...props}>{children}</pre>
    </div>
  )
}

const MARKDOWN_COMPONENTS = {
  a: ({ children, node: _node, href, onOpenFile, ...props }: ComponentPropsWithoutRef<'a'> & { node?: unknown; href?: string; onOpenFile?: (path: string) => void }) => {
    const isFile = Boolean(href && isLocalFileHref(href))
    if (isFile && onOpenFile && href) {
      // 编码留到文件解析层，那里能区分字面 %23 与未编码的章节锚点 #。
      const path = href.replace(/^file:\/\/\/?/i, '').replace(/^#file\//, '')
      return (
        <button type="button" className="zm-file-link" onClick={() => onOpenFile(path)} title={T.agent.openFileHint(decodeFileHref(path))}>
          <FileText aria-hidden />
          <span>{children}</span>
        </button>
      )
    }
    return <a {...props} href={href} target="_blank" rel="noreferrer noopener">{children}</a>
  },
  pre: CodeBlock,
}

const MemoDocument = memo(
  function MemoDocument({ text, onOpenFile }: { text: string; onOpenFile?: (path: string) => void }) {
    const components = useMemo(() => ({
      ...MARKDOWN_COMPONENTS,
      a: (props: ComponentPropsWithoutRef<'a'>) => MARKDOWN_COMPONENTS.a({ ...props, onOpenFile }),
    }), [onOpenFile])
    return (
      <ReactMarkdown
        remarkPlugins={onOpenFile ? [remarkGfm, remarkFileMentions] : [remarkGfm]}
        skipHtml
        urlTransform={(url) => onOpenFile && isLocalFileHref(url) ? url : defaultUrlTransform(url)}
        components={components}
      >
        {text}
      </ReactMarkdown>
    )
  },
  (prev, next) => prev.text === next.text && prev.onOpenFile === next.onOpenFile,
)

/**
 * Agent 的回答按排版渲染：标题、列表、表格、代码块。不渲染 HTML（防注入）；外链新窗口打开、不带 opener；
 * 指向文件的链接（#file/…、file://、.md/.txt）点了在右栏打开。
 * 完整文档交给 CommonMark/GFM 解析，保留跨段引用、列表和代码围栏；原文未变时复用渲染结果。
 */
export function Markdown({ text, streaming = false, onOpenFile }: { text: string; streaming?: boolean; onOpenFile?: (path: string) => void }) {
  const openFileRef = useRef(onOpenFile)
  useLayoutEffect(() => { openFileRef.current = onOpenFile }, [onOpenFile])
  const openLatest = useCallback((path: string) => openFileRef.current?.(path), [])
  return (
    <div className={`zm-md${streaming ? ' is-streaming' : ''}`}>
      <MemoDocument text={text} onOpenFile={onOpenFile ? openLatest : undefined} />
    </div>
  )
}

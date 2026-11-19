import { useEffect, useRef, useState, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { Image as ImageIcon, X } from 'lucide-react'
import { readAgentImage } from '../../api/agentImages'
import type { AgentImageAttachment } from '../../api/agent'
import type { DraftImage } from '../lib/useConversationImages'
import { Spinner } from '../ui'
import './images.css'

function ImageItem({ image, preview, name, children }: { image?: AgentImageAttachment; preview?: string; name: string; children?: ReactNode }) {
  const [source, setSource] = useState<string | null>(preview ?? null)
  const [failed, setFailed] = useState(false)
  const [open, setOpen] = useState(false)
  const button = useRef<HTMLButtonElement | null>(null)
  const close = useRef<HTMLButtonElement | null>(null)
  useEffect(() => {
    if (preview) { setSource(preview); return }
    if (!image?.url) return
    const controller = new AbortController()
    let objectUrl: string | null = null
    setFailed(false)
    void readAgentImage(image.url, controller.signal).then((blob) => {
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob); setSource(objectUrl)
    }).catch(() => { if (!controller.signal.aborted) setFailed(true) })
    return () => { controller.abort(); if (objectUrl) URL.revokeObjectURL(objectUrl) }
  }, [image?.url, preview])
  useEffect(() => {
    if (!open) return
    const key = (event: KeyboardEvent) => {
      if (event.key === 'Escape') { event.preventDefault(); event.stopPropagation(); setOpen(false); button.current?.focus() }
      if (event.key === 'Tab') { event.preventDefault(); close.current?.focus() }
    }
    window.addEventListener('keydown', key, true)
    return () => window.removeEventListener('keydown', key, true)
  }, [open])
  return <div className="zm-image-attachment">
    <button ref={button} type="button" className="zm-image-preview" aria-label={`预览图片：${name}`} disabled={!source || failed} onClick={() => setOpen(true)}>
      {source && !failed ? <img src={source} alt={name} onError={() => setFailed(true)} /> : <span><ImageIcon aria-hidden />{failed ? '图片暂不可用' : '读取图片…'}</span>}
    </button>
    {children}
    {open && source ? createPortal(<div className="zm-image-lightbox" role="dialog" aria-label={`图片预览：${name}`} aria-modal="true" onClick={() => { setOpen(false); button.current?.focus() }}>
      <button ref={close} type="button" autoFocus aria-label="关闭图片预览" onClick={() => { setOpen(false); button.current?.focus() }}><X aria-hidden /></button>
      <figure onClick={(event) => event.stopPropagation()}><img src={source} alt={name} /><figcaption>{name}{image ? ` · ${image.width} × ${image.height}` : ''}</figcaption></figure>
    </div>, button.current?.closest('.zm') ?? document.body) : null}
  </div>
}

export function DraftImages({ images, onRemove, onRetry }: { images: DraftImage[]; onRemove: (id: string) => void; onRetry: (id: string) => void }) {
  if (!images.length) return null
  return <div className="zm-draft-images" aria-label="待发送图片">{images.map((entry) => <ImageItem key={entry.clientId} image={entry.image} preview={entry.preview} name={entry.name}>
    <button type="button" className="zm-image-remove" aria-label={`移除图片：${entry.name}`} onClick={() => onRemove(entry.clientId)}><X aria-hidden /></button>
    <div className="zm-image-state" role="status">{entry.status === 'uploading' ? <><Spinner />上传中</> : entry.status === 'failed' ? <><span>{entry.error}</span><button type="button" onClick={() => onRetry(entry.clientId)}>重试上传</button></> : <span>{entry.name}</span>}</div>
  </ImageItem>)}</div>
}

export function MessageImages({ images }: { images?: AgentImageAttachment[] }) {
  if (!images?.length) return null
  return <div className="zm-message-images">{images.map((entry) => <ImageItem key={entry.image_id} image={entry} name={entry.name} />)}</div>
}

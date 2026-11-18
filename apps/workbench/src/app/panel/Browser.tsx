import { useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { ArrowLeft, ArrowRight, Plus, RotateCw, X } from 'lucide-react'
import { sharedBrowser, type SharedBrowserState } from '../../api/sharedBrowser'
import { Btn, IconBtn } from '../ui'
import './browser.css'

const empty: SharedBrowserState = { tabs: [], active: null, downloads: [], error: null }

export function BrowserPanel({ sessionId }: { sessionId: string | null }) {
  const bridge = sharedBrowser()
  const [state, setState] = useState(empty)
  const [error, setError] = useState<string | null>(null)
  const [address, setAddress] = useState('')
  const [downloadsOpen, setDownloadsOpen] = useState(false)
  const editing = useRef(false)
  const page = useRef<HTMLDivElement>(null)
  const tabs = state.tabs.filter(tab => tab.session_id === sessionId)
  const current = tabs.find(tab => tab.id === state.active) ?? tabs.at(-1)
  const currentId = current?.id
  const identity = useRef(sessionId)
  identity.current = sessionId
  const command = useCallback(async (request: Record<string, unknown>) => {
    const owner = sessionId
    try { const result = await bridge?.command({ ...request, session_id: owner }); if (identity.current === owner) setError(null); return result }
    catch (failure) { if (identity.current === owner) setError(failure instanceof Error ? failure.message : String(failure)) }
  }, [bridge, sessionId])

  useEffect(() => {
    if (!bridge) return
    let alive = true, changes = 0
    const off = bridge.onChange(next => { changes++; if (alive) setState(next) })
    void bridge.state().then(next => { if (alive && changes === 0) setState(next) }, failure => { if (alive) setError(String(failure)) })
    return () => { alive = false; off() }
  }, [bridge])
  useEffect(() => { editing.current = false; setError(null); setAddress('') }, [sessionId])
  useEffect(() => { if (!editing.current) setAddress(current?.url === 'about:blank' ? '' : current?.url ?? '') }, [current?.id, current?.url])
  useEffect(() => { if (current && current.id !== state.active) void command({ action: 'select', id: current.id }) }, [command, current, state.active])

  useLayoutEffect(() => {
    const element = page.current
    if (!bridge || !element) return
    let frame = 0
    const layout = () => {
      cancelAnimationFrame(frame)
      frame = requestAnimationFrame(() => {
        const rectangle = element.getBoundingClientRect()
        const obscured = Boolean(document.querySelector('dialog[open], [role="menu"], [aria-modal="true"]'))
        const visible = element.checkVisibility?.() ?? Boolean(element.getClientRects().length)
        void bridge.command({ action: 'layout', session_id: sessionId, bounds: visible && !obscured && currentId && rectangle.width > 0 && rectangle.height > 0
          ? { x: rectangle.x, y: rectangle.y, width: rectangle.width, height: rectangle.height } : null }).catch(failure => setError(String(failure)))
      })
    }
    const resize = new ResizeObserver(layout); resize.observe(element)
    // Native child views must yield to menus/dialogs and hidden panes; CSS z-index cannot cover them.
    const mutation = new MutationObserver(layout)
    mutation.observe(document.body, { childList: true, subtree: true, attributes: true, attributeFilter: ['open', 'hidden', 'class', 'style'] })
    const off = bridge.onLayout(layout)
    window.addEventListener('resize', layout); window.addEventListener('scroll', layout, true); layout()
    return () => { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); off(); window.removeEventListener('resize', layout); window.removeEventListener('scroll', layout, true); void bridge.command({ action: 'layout', bounds: null }).catch(() => {}) }
  }, [bridge, currentId, sessionId, downloadsOpen])

  if (!bridge) return <div className="zm-browser-empty"><p>共享浏览器需要从织梦桌面入口打开。</p><p>桌面里会显示 Agent 正在操作的同一个页面，可以随时接手。</p></div>
  if (!sessionId) return <div className="zm-browser-empty">先选择一条对话，再打开网页。</div>
  const downloads = state.downloads.filter(item => item.session_id === sessionId)
  const navigate = (action: string) => { if (current) void command({ action, id: current.id }) }
  return <section className="zm-browser" aria-label="共享浏览器">
    <div className="zm-browser-tabs" aria-label="当前对话的网页">
      {tabs.map(tab => <button key={tab.id} type="button" aria-pressed={current?.id === tab.id} title={tab.url}
        onClick={() => void command({ action: 'select', id: tab.id })}>{tab.loading ? '正在打开…' : tab.title}</button>)}
      <IconBtn label="新建浏览器页面" icon={<Plus />} onClick={() => void command({ action: 'create', url: 'about:blank' })} />
    </div>
    <form className="zm-browser-address" onSubmit={event => { event.preventDefault(); editing.current = false; void command({ action: current ? 'navigate' : 'create', id: current?.id, url: address }) }}>
      <IconBtn label="浏览器后退" icon={<ArrowLeft />} disabled={!current?.can_back} onClick={() => navigate('back')} />
      <IconBtn label="浏览器前进" icon={<ArrowRight />} disabled={!current?.can_forward} onClick={() => navigate('forward')} />
      <IconBtn label="刷新网页" icon={<RotateCw />} disabled={!current} onClick={() => navigate('reload')} />
      <input aria-label="网页地址" placeholder="输入网页地址" value={address} onFocus={() => { editing.current = true }} onBlur={() => { editing.current = false }} onChange={event => setAddress(event.target.value)} />
      <Btn type="submit" size="sm">打开</Btn>
      <IconBtn label="关闭当前网页" icon={<X />} disabled={!current} onClick={() => void command({ action: 'close', id: current?.id })} />
    </form>
    {current ? <div className="zm-browser-control"><span>{current.mode === 'agent' ? 'Agent 可操作此页，你可随时接手' : '你正在操作此页'}</span>
      <Btn size="sm" onClick={() => void command({ action: 'control', id: current.id, mode: current.mode === 'agent' ? 'user' : 'agent' })}>{current.mode === 'agent' ? '我来操作' : '交给 Agent'}</Btn></div> : null}
    {current?.restored ? <p className="zm-browser-note">已恢复网址，重启前的未提交表单没有恢复。</p> : null}
    {error || current?.error || state.error ? <p role="alert" className="zm-browser-error">{error || current?.error || state.error}</p> : null}
    <div ref={page} className="zm-browser-page">{!current ? <p>打开网页，或让 Agent 在这条对话里开始浏览。</p> : null}</div>
    <details className="zm-browser-downloads" onToggle={event => setDownloadsOpen(event.currentTarget.open)}><summary>下载记录{downloads.length ? `（${downloads.length}）` : ''}</summary>
      {downloads.length ? downloads.map(item => <div key={item.id}><strong>{item.name}</strong><span>{item.status === 'completed' ? '已保存' : item.status === 'progressing' ? '下载中' : item.status === 'interrupted' ? '下载中断' : '未确认完成'} · {item.bytes} 字节</span><span>{item.path}</span>{item.status === 'completed' ? <Btn size="sm" variant="ghost" onClick={() => void command({ action: 'reveal-download', id: item.id })}>在文件夹中查看</Btn> : null}</div>) : <p>还没有下载文件。</p>}
    </details>
  </section>
}

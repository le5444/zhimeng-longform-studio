import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { Profiler, createRef } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { AgentMessage } from '../../api/agent'
import { ConversationRail } from '../chat/ConversationRail'
import { useConversationScroll } from '../chat/useConversationScroll'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const messages = [
  { message_id: 'u1', role: 'user', content: '整理预算表', session_id: 's1' },
  { message_id: 'a1', role: 'assistant', content: '已保存 summary.csv。', session_id: 's1' },
  { message_id: 'u2', role: 'user', content: '再核对合计', session_id: 's1' },
] as AgentMessage[]
function Fixture({ load = async () => {}, latest = () => {}, earlier = false, items = messages }: {
  load?: () => Promise<void>; latest?: () => void; earlier?: boolean; items?: AgentMessage[]
}) {
  const { scrollRef: ref, loadEarlier, earlier: state } = useConversationScroll(items[0]?.session_id ?? '', items, load)
  return <div className="zm"><div ref={ref} data-testid="scroller">{items.map((item) => <div data-message-id={item.message_id} key={item.message_id}>{item.content}</div>)}</div>
    <ConversationRail messages={items} scrollRef={ref} onLoadEarlier={loadEarlier} hasEarlier={earlier} earlier={state} onLatest={latest} />
    <textarea aria-label="原草稿" defaultValue="保留这个输入" /></div>
}
describe('桌面对话历史刻度', () => {
  it('长历史滚动只测少量位置，补入旧消息后重新建立刻度定位', () => {
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const scroller = document.createElement('div')
    Object.defineProperties(scroller, { offsetTop: { value: 64 }, clientHeight: { value: 500 } })
    let scrollOffset = 0
    const measure = vi.fn()
    const items = Array.from({ length: 1024 }, (_, index) => ({ ...messages[0], message_id: `u-${index}`, content: `提问 ${index}` }))
    for (const [index, item] of items.entries()) {
      const node = document.createElement('div')
      node.dataset.messageId = item.message_id
      node.getBoundingClientRect = () => { measure(); return { top: index * 100 - scrollOffset } as DOMRect }
      scroller.append(node)
    }
    const ref = createRef<HTMLDivElement>()
    ref.current = scroller
    const props = { scrollRef: ref, hasEarlier: false, earlier: 'idle' as const, onLoadEarlier: async () => {}, onLatest: () => {} }
    const view = render(<ConversationRail messages={items} {...props} />)
    measure.mockClear()
    scrollOffset = 90_020
    act(() => { scroller.dispatchEvent(new Event('scroll')); frame(0) })
    expect(measure.mock.calls.length).toBeLessThanOrEqual(11)
    expect(screen.getByRole('button', { name: '跳到第 901 条消息：提问 900' })).toHaveAttribute('aria-current', 'location')
    const older = document.createElement('div')
    older.dataset.messageId = 'older'
    older.getBoundingClientRect = () => ({ top: -100 - scrollOffset }) as DOMRect
    scroller.prepend(older)
    view.rerender(<ConversationRail messages={[{ ...messages[0], message_id: 'older', content: '最早提问' }, ...items]} {...props} />)
    scroller.scrollTo = vi.fn()
    fireEvent.click(screen.getByRole('button', { name: '跳到第 1 条消息：最早提问' }))
    expect(scroller.scrollTo).toHaveBeenCalledOnce()
  })

  it('正文变长但刻度布局和位置未变时不重新绘制，真实滚动与尺寸变化仍更新', () => {
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const scroller = document.createElement('div')
    Object.defineProperties(scroller, { offsetTop: { value: 64 }, clientHeight: { value: 500, configurable: true } })
    let secondTop = 200
    for (const [id, top] of [['u1', 0], ['u2', 200]] as const) {
      const node = document.createElement('div')
      node.dataset.messageId = id
      node.getBoundingClientRect = () => ({ top: id === 'u2' ? secondTop : top }) as DOMRect
      scroller.append(node)
    }
    const ref = createRef<HTMLDivElement>()
    ref.current = scroller
    const committed = vi.fn()
    render(<Profiler id="rail" onRender={committed}><ConversationRail messages={messages} scrollRef={ref}
      hasEarlier={false} earlier="idle" onLoadEarlier={async () => {}} onLatest={() => {}} /></Profiler>)
    const first = screen.getByRole('button', { name: '跳到第 1 条消息：整理预算表' })
    expect(first).toHaveAttribute('aria-current', 'location')
    // Warm an identical measurement before counting; React may check an update once before bailing out.
    act(() => { scroller.dispatchEvent(new Event('scroll')); frame(0) })
    committed.mockClear()
    act(() => { scroller.dispatchEvent(new Event('scroll')); frame(0) })
    expect(committed).not.toHaveBeenCalled()
    secondTop = 10
    act(() => { scroller.dispatchEvent(new Event('scroll')); frame(0) })
    expect(screen.getByRole('button', { name: '跳到第 2 条消息：再核对合计' })).toHaveAttribute('aria-current', 'location')
    Object.defineProperty(scroller, 'clientHeight', { value: 650 })
    act(() => { window.dispatchEvent(new Event('resize')); frame(0) })
    expect(screen.getByRole('navigation')).toHaveStyle({ top: '72px', height: '634px' })
  })

  it('悬停预览原问与回答，点刻度定位且不动草稿，最新按钮可用', () => {
    const latest = vi.fn()
    render(<Fixture latest={latest} />)
    const scroll = vi.fn()
    screen.getByTestId('scroller').scrollTo = scroll
    const mark = screen.getByRole('button', { name: '跳到第 1 条消息：整理预算表' })
    fireEvent.mouseEnter(mark)
    expect(screen.getByRole('tooltip')).toHaveTextContent('已保存 summary.csv。')
    fireEvent.click(mark)
    expect(scroll).toHaveBeenCalledWith(expect.objectContaining({ behavior: 'instant' }))
    expect(screen.queryByRole('tooltip')).toBeNull()
    expect(screen.getByRole('textbox', { name: '原草稿' })).toHaveValue('保留这个输入')
    fireEvent.click(screen.getByRole('button', { name: '回到最新消息' }))
    expect(latest).toHaveBeenCalledOnce()
  })

  it('键盘能查看预览和退出；没有回复时照实说明', () => {
    render(<Fixture />)
    const mark = screen.getByRole('button', { name: '跳到第 2 条消息：再核对合计' })
    fireEvent.focus(mark)
    expect(screen.getByRole('tooltip')).toHaveTextContent('尚无回复')
    fireEvent.keyDown(mark, { key: 'Escape' })
    expect(screen.queryByRole('tooltip')).toBeNull()
  })

  it('更早消息失败可再次加载；只有一条时不增加导航', async () => {
    const load = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValue(undefined)
    const view = render(<Fixture earlier load={load} />)
    fireEvent.click(screen.getByRole('button', { name: '加载更早的消息' }))
    fireEvent.click(await screen.findByRole('button', { name: '重试加载更早的消息' }))
    await waitFor(() => expect(load).toHaveBeenCalledTimes(2))
    view.rerender(<Fixture items={messages.slice(0, 1)} />)
    expect(screen.queryByRole('navigation', { name: '对话历史定位' })).toBeNull()
  })
})

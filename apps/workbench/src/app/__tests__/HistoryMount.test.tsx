import { StrictMode, createRef } from 'react'
import { act, cleanup, render, screen } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { HistoryMount } from '../chat/HistoryMount'

beforeEach(() => vi.useFakeTimers())
afterEach(() => { cleanup(); vi.useRealTimers(); vi.restoreAllMocks() })

function messages(count: number, prefix = 'a') {
  return Array.from({ length: count }, (_, i) => <div key={`${prefix}${i}`} data-message-id={`${prefix}${i}`}>
    <pre><code>{`${prefix} code ${i}`}</code></pre><button>{`${prefix} copy ${i}`}</button>
  </div>)
}

function fixture(height = 100, rounded = false) {
  const scroller = document.createElement('div')
  document.body.append(scroller)
  const ref = createRef<HTMLDivElement>()
  ref.current = scroller
  Object.defineProperties(scroller, {
    clientHeight: { value: 300 },
    scrollHeight: { get: () => scroller.querySelectorAll('[data-message-id]').length * height },
  })
  if (rounded) {
    let top = 0
    Object.defineProperty(scroller, 'scrollTop', { get: () => top, set: (value: number) => { top = Math.round(value) } })
  }
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    const index = [...scroller.querySelectorAll('[data-message-id]')].indexOf(this)
    const top = index < 0 ? 0 : index * height - scroller.scrollTop
    return { top, bottom: top + height, height } as DOMRect
  })
  const onReady = vi.fn()
  const view = (items: ReturnType<typeof messages>, scope = 'a') => <StrictMode><HistoryMount key={scope} scrollRef={ref} onReady={onReady}>{items}</HistoryMount></StrictMode>
  return { scroller, onReady, view }
}

async function batch() { await act(async () => { vi.advanceTimersByTime(24) }) }
async function finish() { for (let i = 0; i < 10; i++) await batch() }

describe('initial history mounting', () => {
  it('逐批自动显示全部历史，保留已挂载代码节点、选区和按钮焦点', async () => {
    const f = fixture()
    const items = messages(50)
    render(f.view(items), { container: f.scroller })
    expect(screen.queryByText('a code 0')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent('正在显示历史记录')
    const code = screen.getByText('a code 49')
    const button = screen.getByRole('button', { name: 'a copy 49' })
    button.focus()
    const range = document.createRange()
    range.setStart(code.firstChild!, 0)
    range.setEnd(code.firstChild!, code.textContent!.length)
    window.getSelection()!.removeAllRanges()
    window.getSelection()!.addRange(range)
    expect(window.getSelection()!.toString()).toBe('a code 49')
    await finish()
    expect(f.scroller.querySelectorAll('[data-message-id]')).toHaveLength(50)
    expect(screen.getByText('a code 49')).toBe(code)
    expect(button).toHaveFocus()
    expect(window.getSelection()!.toString()).toBe('a code 49')
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(f.onReady).toHaveBeenCalledTimes(1)
  })

  it('补入期间新消息和旧消息修正立即出现，不挤掉已显示的首条', async () => {
    const f = fixture()
    const items = messages(50)
    const result = render(f.view(items), { container: f.scroller })
    const first = f.scroller.querySelector('[data-message-id]')!
    const next = [...items, ...messages(2, 'new')]
    next[49] = <div key="a49" data-message-id="a49">corrected answer</div>
    result.rerender(f.view(next))
    expect(screen.getByText('new code 1')).toBeInTheDocument()
    expect(screen.getByText('corrected answer')).toBeInTheDocument()
    expect(f.scroller.querySelector('[data-message-id]')).toBe(first)
    await finish()
    expect(f.scroller.querySelectorAll('[data-message-id]')).toHaveLength(52)
    expect(screen.getByText('a code 0')).toBeInTheDocument()
  })

  it('提交前按用户最新阅读位置恢复，尾部新增不计入头部位移', async () => {
    const f = fixture()
    const items = messages(50)
    const result = render(f.view(items), { container: f.scroller })
    f.scroller.scrollTop = 150
    // A different scroll after scheduling must win. A concurrent tail adds 200px.
    f.scroller.scrollTop = 225
    result.rerender(f.view([...items, ...messages(2, 'new')]))
    const anchor = f.scroller.querySelector('[data-message-id="a40"]')!
    const offset = anchor.getBoundingClientRect().top
    await batch()
    expect(anchor.getBoundingClientRect().top).toBe(offset)
    expect(f.scroller.scrollTop).toBe(1425)
    f.scroller.scrollTop += 75
    const moved = anchor.getBoundingClientRect().top
    await batch()
    expect(anchor.getBoundingClientRect().top).toBe(moved)
  })

  it('读最新时随分批保持尾部，完成后补页同步交还原滚动控制', async () => {
    const f = fixture()
    const items = messages(26)
    const result = render(f.view(items), { container: f.scroller })
    f.scroller.scrollTop = f.scroller.scrollHeight - 300
    await batch()
    // jsdom does not clamp scrollTop; a real browser clamps this to the bottom.
    expect(f.scroller.scrollTop).toBe(f.scroller.scrollHeight)
    await finish()
    f.scroller.scrollTop = 123
    result.rerender(f.view([...messages(20, 'earlier'), ...items]))
    expect(f.scroller.querySelectorAll('[data-message-id]')).toHaveLength(46)
    expect(screen.queryByRole('status')).not.toBeInTheDocument()
    expect(f.scroller.scrollTop).toBe(123)
  })

  it('滚动位置取整时不逐批累积小数漂移，后来主动滚动仍优先', async () => {
    const f = fixture(100.03, true)
    render(f.view(messages(110)), { container: f.scroller })
    f.scroller.scrollTop = 225
    const anchor = f.scroller.querySelector('[data-message-id="a100"]')!
    const initial = anchor.getBoundingClientRect().top
    for (let i = 0; i < 4; i++) await batch()
    expect(Math.abs(anchor.getBoundingClientRect().top - initial)).toBeLessThan(0.51)
    f.scroller.scrollTop += 75
    const moved = anchor.getBoundingClientRect().top
    await finish()
    expect(Math.abs(anchor.getBoundingClientRect().top - moved)).toBeLessThan(0.51)
  })

  it('A 到 B 再到 A 取消旧调度，不恢复另一个会话的节点', async () => {
    const f = fixture()
    const result = render(f.view(messages(50)), { container: f.scroller })
    await batch()
    result.rerender(f.view(messages(2, 'b'), 'b'))
    await batch()
    expect(f.scroller.querySelectorAll('[data-message-id]')).toHaveLength(2)
    expect(screen.queryByText('a code 49')).not.toBeInTheDocument()
    result.rerender(f.view(messages(50)))
    await finish()
    expect(f.scroller.querySelectorAll('[data-message-id]')).toHaveLength(50)
    expect(screen.queryByText('b code 1')).not.toBeInTheDocument()
    expect(vi.getTimerCount()).toBe(0)
  })
})

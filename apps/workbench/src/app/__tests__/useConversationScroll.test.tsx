import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { useConversationScroll } from '../chat/useConversationScroll'

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

function deferred() {
  let resolve!: () => void
  let reject!: (error: Error) => void
  const promise = new Promise<void>((yes, no) => { resolve = yes; reject = no })
  return { promise, resolve, reject }
}

function scroller() {
  const node = document.createElement('div')
  Object.defineProperties(node, { scrollHeight: { value: 1000, configurable: true }, clientHeight: { value: 400 } })
  node.scrollTop = 200
  return node
}

describe('conversation reading position', () => {
  it('屏外排版向下修正滚动位置时继续跟随，用户主动向上仍可离开尾部', () => {
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, async () => {}))
    const node = scroller()
    view.result.current.scrollRef.current = node
    node.scrollTop = 600
    act(() => view.result.current.onScroll())
    Object.defineProperty(node, 'scrollHeight', { value: 2000 })
    node.scrollTop = 1000
    act(() => view.result.current.onScroll())
    expect(view.result.current.atBottom).toBe(true)
    act(() => frame(0))
    expect(node.scrollTop).toBe(2000)
    node.scrollTop = 800
    act(() => view.result.current.onScroll())
    expect(view.result.current.atBottom).toBe(false)
    act(() => frame(0))
    expect(node.scrollTop).toBe(800)
  })

  it('回到最新直接跳过长历史，下一帧按展开后的真实尾部高度校准', () => {
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, async () => {}))
    const node = scroller()
    node.scrollTo = vi.fn()
    view.result.current.scrollRef.current = node
    act(() => view.result.current.onScroll())
    act(() => view.result.current.toBottom())
    expect(node.scrollTo).toHaveBeenCalledWith({ top: 1000, behavior: 'instant' })
    Object.defineProperty(node, 'scrollHeight', { value: 1400 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(1400)
    expect(view.result.current.atBottom).toBe(true)
  })

  it('补历史期间尾部继续增长，仍留在原来读的消息位置', async () => {
    const request = deferred()
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, () => request.promise))
    const node = scroller()
    const message = document.createElement('div')
    message.dataset.messageId = 'reading'
    let documentTop = 150
    message.getBoundingClientRect = () => ({ top: documentTop - node.scrollTop, bottom: documentTop + 300 - node.scrollTop }) as DOMRect
    node.append(message)
    view.result.current.scrollRef.current = node
    act(() => { void view.result.current.loadEarlier() })
    await act(async () => request.resolve())
    // 头部补入 400px，尾部同时增长 250px。
    documentTop += 400
    Object.defineProperty(node, 'scrollHeight', { value: 1650 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(600)
  })

  it('shares one earlier request and restores the current paragraph after prepend', async () => {
    const request = deferred()
    const fetch = vi.fn(() => request.promise)
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, fetch))
    const node = scroller()
    view.result.current.scrollRef.current = node
    act(() => { void view.result.current.loadEarlier(); void view.result.current.loadEarlier() })
    expect(fetch).toHaveBeenCalledTimes(1)
    expect(view.result.current.earlier).toBe('loading')
    await act(async () => request.resolve())
    Object.defineProperty(node, 'scrollHeight', { value: 1400 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(600)
    expect(view.result.current.earlier).toBe('idle')
    expect(view.result.current.atBottom).toBe(false)
  })

  it('等待旧记录时用户继续滚动，以后来阅读的位置为准', async () => {
    const request = deferred()
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, () => request.promise))
    const node = scroller()
    view.result.current.scrollRef.current = node
    act(() => { void view.result.current.loadEarlier() })
    node.scrollTop = 300
    act(() => view.result.current.onScroll())
    await act(async () => request.resolve())
    Object.defineProperty(node, 'scrollHeight', { value: 1400 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(700)
  })

  it('等待旧记录时回到最新，补页结束不把用户拉回旧位置', async () => {
    const request = deferred()
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(() => useConversationScroll('a', 1, () => request.promise))
    const node = scroller()
    node.scrollTo = vi.fn()
    view.result.current.scrollRef.current = node
    act(() => { void view.result.current.loadEarlier(); view.result.current.toBottom() })
    await act(async () => request.resolve())
    Object.defineProperty(node, 'scrollHeight', { value: 1400 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(1400)
  })

  it('a late failure cannot replace the new conversation loading state', async () => {
    const a = deferred()
    const b = deferred()
    const view = renderHook(({ scope, fetch }) => useConversationScroll(scope, 1, fetch), {
      initialProps: { scope: 'a', fetch: () => a.promise },
    })
    act(() => { void view.result.current.loadEarlier() })
    view.rerender({ scope: 'b', fetch: () => b.promise })
    expect(view.result.current.earlier).toBe('idle')
    act(() => { void view.result.current.loadEarlier() })
    await act(async () => a.reject(new Error('old connection')))
    expect(view.result.current.earlier).toBe('loading')
    await act(async () => b.reject(new Error('current connection')))
    expect(view.result.current.earlier).toBe('failed')
  })

  it('a scheduled old restoration cannot move the viewport after switching away and back', async () => {
    const request = deferred()
    let oldFrame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { oldFrame = callback; return 1 })
    const view = renderHook(({ scope }) => useConversationScroll(scope, 1, () => request.promise), { initialProps: { scope: 'a' } })
    const node = scroller()
    view.result.current.scrollRef.current = node
    act(() => { void view.result.current.loadEarlier() })
    await act(async () => request.resolve())
    const restoreOld = oldFrame
    view.rerender({ scope: 'b' })
    view.rerender({ scope: 'a' })
    node.scrollTop = 50
    act(() => restoreOld(0))
    expect(node.scrollTop).toBe(50)
    expect(view.result.current.earlier).toBe('idle')
  })

  it('new output preserves an upward reading position and follows again at the bottom', () => {
    let frame!: FrameRequestCallback
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => { frame = callback; return 1 })
    const view = renderHook(({ version }) => useConversationScroll('a', version, async () => {}), { initialProps: { version: 1 } })
    const node = scroller()
    view.result.current.scrollRef.current = node
    act(() => view.result.current.onScroll())
    view.rerender({ version: 2 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(200)
    node.scrollTop = 600
    act(() => view.result.current.onScroll())
    Object.defineProperty(node, 'scrollHeight', { value: 1400 })
    view.rerender({ version: 3 })
    act(() => frame(0))
    expect(node.scrollTop).toBe(1400)
  })

  it('连续更新每帧只测一次高度，绘制前向上阅读或换会话取消跟随', () => {
    const frames: FrameRequestCallback[] = []
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback))
    const view = renderHook(({ scope, version }) => useConversationScroll(scope, version, async () => {}), {
      initialProps: { scope: 'a', version: 1 },
    })
    const node = scroller()
    const height = vi.fn(() => 1400)
    Object.defineProperty(node, 'scrollHeight', { get: height })
    view.result.current.scrollRef.current = node
    view.rerender({ scope: 'a', version: 2 })
    view.rerender({ scope: 'a', version: 3 })
    expect(frames).toHaveLength(1)
    expect(height).not.toHaveBeenCalled()
    act(() => frames[0](0))
    expect(height).toHaveBeenCalledOnce()
    expect(node.scrollTop).toBe(1400)
    view.rerender({ scope: 'a', version: 4 })
    node.scrollTop = 100
    act(() => view.result.current.onScroll())
    act(() => frames[1](0))
    expect(node.scrollTop).toBe(100)
    view.rerender({ scope: 'b', version: 1 })
    view.rerender({ scope: 'a', version: 5 })
    act(() => frames[2](0))
    expect(node.scrollTop).toBe(100)
    act(() => frames[3](0))
    expect(node.scrollTop).toBe(1400)
  })

  it('容器或正文尺寸改变时继续跟随尾部，手动阅读不被拉走，旧观察失效', () => {
    const frames: FrameRequestCallback[] = []
    const resized: Array<() => void> = []
    const disconnect = vi.fn()
    const observe = vi.fn()
    vi.stubGlobal('requestAnimationFrame', (callback: FrameRequestCallback) => frames.push(callback))
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { resized.push(callback) }
      observe = observe
      disconnect = disconnect
    })
    const view = renderHook(({ scope }) => useConversationScroll(scope, 1, async () => {}), { initialProps: { scope: 'a' } })
    const node = scroller()
    node.append(document.createElement('div'))
    view.result.current.scrollRef.current = node
    view.rerender({ scope: 'b' })
    expect(observe.mock.calls.map(call => call[0])).toEqual([node, node.firstElementChild])
    act(() => frames.at(-1)!(0))
    Object.defineProperty(node, 'scrollHeight', { value: 2000, configurable: true })
    act(() => resized[0]())
    act(() => frames.at(-1)!(0))
    expect(node.scrollTop).toBe(2000)
    node.scrollTop = 100
    act(() => view.result.current.onScroll())
    const count = frames.length
    Object.defineProperty(node, 'scrollHeight', { value: 2400 })
    act(() => resized[0]())
    expect(frames).toHaveLength(count)
    expect(node.scrollTop).toBe(100)
    expect(view.result.current.atBottom).toBe(false)
    view.rerender({ scope: 'c' })
    const nextCount = frames.length
    act(() => resized[0]())
    expect(frames).toHaveLength(nextCount)
    expect(disconnect).toHaveBeenCalledOnce()
  })
})

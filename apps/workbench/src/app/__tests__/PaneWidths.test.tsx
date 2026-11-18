import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { PaneResizeHandle } from '../PaneResizeHandle'
import { usePaneWidths } from '../lib/usePaneWidths'
import { WorkbenchLayout } from '../WorkbenchLayout'

const originalWidth = window.innerWidth
function resize(width: number) {
  Object.defineProperty(window, 'innerWidth', { configurable: true, value: width })
  window.dispatchEvent(new Event('resize'))
}
beforeEach(() => { localStorage.clear(); resize(1440) })
afterEach(() => { cleanup(); vi.restoreAllMocks(); resize(originalWidth) })

it('opening the panel in a smaller desktop window preserves usable chat width', () => {
  resize(1024)
  const { result, rerender } = renderHook(({ open }) => usePaneWidths(open, false, false), { initialProps: { open: false } })
  expect(result.current.side.value).toBe(260)
  rerender({ open: true })
  expect(result.current.side.value).toBe(224)
  expect(result.current.panel.value).toBe(320)
  expect(1024 - result.current.side.value - result.current.panel.value).toBe(480)
  rerender({ open: false })
  expect(result.current.side.value).toBe(260)
})

it('window constraints do not overwrite saved widths; wide mode also fits', () => {
  localStorage.setItem('zhimeng.app.side-width', '330')
  localStorage.setItem('zhimeng.app.panel-width', '700')
  resize(1200)
  const { result, rerender } = renderHook(({ wide }) => usePaneWidths(true, wide, false), { initialProps: { wide: false } })
  expect(result.current.panel.value).toBe(390)
  act(() => resize(1600))
  expect(result.current.side.value).toBe(330)
  expect(result.current.panel.value).toBe(700)
  act(() => resize(901))
  expect(901 - result.current.side.value - result.current.panel.value).toBeGreaterThanOrEqual(380)
  rerender({ wide: true })
  expect(901 - result.current.side.value - result.current.panel.value).toBeGreaterThanOrEqual(380)
  act(() => resize(1920))
  expect(result.current.panel.value).toBeGreaterThan(900)
  expect(localStorage.getItem('zhimeng.app.panel-width')).toBe('700')
})

function Handles() {
  const widths = usePaneWidths(true, false, false)
  return <><PaneResizeHandle pane="side" label="sidebar" {...widths.side} /><PaneResizeHandle pane="panel" label="panel" {...widths.panel} /></>
}

function pointerCapture(node: HTMLElement) {
  Object.defineProperties(node, {
    setPointerCapture: { value: vi.fn(), configurable: true },
    hasPointerCapture: { value: () => true, configurable: true },
    releasePointerCapture: { value: vi.fn(), configurable: true },
  })
}

it('dragging updates width, saves on release, and restores it on remount', () => {
  const view = render(<Handles />)
  const handle = screen.getByRole('separator', { name: 'sidebar' })
  pointerCapture(handle)
  fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 260 })
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: 312 })
  expect(handle).toHaveAttribute('aria-valuenow', '312')
  expect(localStorage.getItem('zhimeng.app.side-width')).toBeNull()
  fireEvent.pointerUp(handle, { pointerId: 1, clientX: 312 })
  expect(localStorage.getItem('zhimeng.app.side-width')).toBe('312')
  view.unmount()
  render(<Handles />)
  expect(screen.getByRole('separator', { name: 'sidebar' })).toHaveAttribute('aria-valuenow', '312')
})

it.each(['escape', 'pointercancel'])('cancelling a drag with %s restores the original preference', method => {
  localStorage.setItem('zhimeng.app.side-width', '330')
  resize(1024)
  render(<Handles />)
  const handle = screen.getByRole('separator', { name: 'sidebar' })
  pointerCapture(handle)
  fireEvent.pointerDown(handle, { button: 0, pointerId: 1, clientX: 224 })
  fireEvent.pointerMove(handle, { pointerId: 1, clientX: 200 })
  expect(handle).toHaveAttribute('aria-valuenow', '200')
  if (method === 'escape') fireEvent.keyDown(handle, { key: 'Escape' })
  else fireEvent.pointerCancel(handle, { pointerId: 1 })
  act(() => resize(1440))
  expect(handle).toHaveAttribute('aria-valuenow', '330')
  expect(localStorage.getItem('zhimeng.app.side-width')).toBe('330')
})

it('keyboard resizing follows the right panel edge, clamps and resets', () => {
  render(<Handles />)
  const handle = screen.getByRole('separator', { name: 'panel' })
  fireEvent.keyDown(handle, { key: 'ArrowLeft' })
  expect(handle).toHaveAttribute('aria-valuenow', '456')
  fireEvent.keyDown(handle, { key: 'Home' })
  expect(handle).toHaveAttribute('aria-valuenow', '320')
  fireEvent.keyDown(handle, { key: 'End' })
  expect(handle).toHaveAttribute('aria-valuenow', '700')
  fireEvent.doubleClick(handle)
  expect(handle).toHaveAttribute('aria-valuenow', '440')
  expect(localStorage.getItem('zhimeng.app.panel-width')).toBe('440')
})

it('resizing keeps the conversation subtree mounted without rerendering it', () => {
  const chatRender = vi.fn()
  function Chat() { chatRender(); return <textarea defaultValue="unsent draft" /> }
  render(<WorkbenchLayout sideCollapsed={false} panelShown panelWide={false} settingsOpen={false}><Chat /></WorkbenchLayout>)
  const handle = screen.getByRole('separator', { name: '调整右栏宽度' })
  fireEvent.keyDown(handle, { key: 'ArrowLeft' })
  fireEvent.keyDown(handle, { key: 'ArrowLeft' })
  expect(handle).toHaveAttribute('aria-valuenow', '472')
  expect(screen.getByRole('textbox')).toHaveValue('unsent draft')
  expect(chatRender).toHaveBeenCalledOnce()
})

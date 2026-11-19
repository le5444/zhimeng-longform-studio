import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ProcessGroup } from '../chat/Message'

afterEach(cleanup)

it('closed process keeps the same subtree without reconciling new history children', () => {
  const states = new Map<string, boolean>()
  const Content = () => <input aria-label="Retained process" defaultValue="draft" />
  const child = <Content />
  const view = render(<ProcessGroup stateKey="run" states={states}>{child}</ProcessGroup>)
  const content = view.container.querySelector('.zm-process-content')!
  expect(content).toBeEmptyDOMElement()
  view.rerender(<ProcessGroup stateKey="run" states={states}><Content /></ProcessGroup>)
  expect(content).toBeEmptyDOMElement()
  fireEvent.click(view.container.querySelector('summary')!)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: 'edited' } })
  fireEvent.click(view.container.querySelector('summary')!)
  expect(input).toHaveValue('edited')
  fireEvent.click(view.container.querySelector('summary')!)
  expect(screen.getByRole('textbox')).toHaveValue('edited')
})

it('defers unopened history content until expansion, then keeps its state', async () => {
  const load = vi.fn(async () => {})
  const renderContent = vi.fn()
  function Content() {
    renderContent()
    return <input aria-label="Detail note" defaultValue="original" />
  }
  const view = render(<ProcessGroup onLoad={load}><Content /></ProcessGroup>)
  expect(renderContent).not.toHaveBeenCalled()
  expect(load).not.toHaveBeenCalled()
  expect(screen.queryByRole('textbox')).toBeNull()
  const summary = view.container.querySelector('summary')!
  fireEvent.click(summary)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: 'retained' } })
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  fireEvent.click(summary)
  expect(view.container.querySelector('input')).toBe(input)
  fireEvent.click(summary)
  expect(screen.getByRole('textbox')).toBe(input)
  expect(input).toHaveValue('retained')
  expect(load).toHaveBeenCalledOnce()
})

it('renders and loads a saved expanded process immediately', async () => {
  const states = new Map([['run-1', true]])
  const load = vi.fn(async () => {})
  const view = render(<ProcessGroup stateKey="run-1" states={states} onLoad={load}><p>Saved process</p></ProcessGroup>)
  expect(screen.getByText('Saved process')).toBeVisible()
  await waitFor(() => expect(load).toHaveBeenCalledOnce())
  fireEvent.click(view.container.querySelector('summary')!)
  expect(states.get('run-1')).toBe(false)
})

it('keeps the content mounted when an automatically opened live process finishes', () => {
  const view = render(<ProcessGroup><input aria-label="Live detail" /></ProcessGroup>)
  view.rerender(<ProcessGroup initiallyOpen><input aria-label="Live detail" /></ProcessGroup>)
  const input = screen.getByRole('textbox')
  fireEvent.change(input, { target: { value: 'expanded detail' } })
  view.rerender(<ProcessGroup><input aria-label="Live detail" /></ProcessGroup>)
  expect(view.container.querySelector('input')).toBe(input)
  fireEvent.click(view.container.querySelector('summary')!)
  expect(screen.getByRole('textbox')).toHaveValue('expanded detail')
})

it('can retry failed history loading without collapsing its content', async () => {
  const load = vi.fn().mockRejectedValueOnce(new Error('offline')).mockResolvedValueOnce(undefined)
  render(<ProcessGroup initiallyOpen onLoad={load}><p>Available process</p></ProcessGroup>)
  fireEvent.click(await screen.findByRole('button', { name: '过程暂未读取，点击重试' }))
  await waitFor(() => expect(screen.queryByText('正在读取处理过程…')).toBeNull())
  expect(load).toHaveBeenCalledTimes(2)
  expect(screen.getByText('Available process')).toBeVisible()
})

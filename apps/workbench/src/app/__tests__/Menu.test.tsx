import { cleanup, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, expect, it, vi } from 'vitest'
import { Menu } from '../ui'

afterEach(cleanup)
it('弹出菜单留在主题根节点里，选项可点，Escape 关闭', async () => {
  const selected = vi.fn()
  const { container } = render(<div className="zm" data-theme="dark"><Menu label="更多" items={[{ key: 'rename', label: '改名', onSelect: selected }]} trigger={({ ref, toggle }) => <button ref={ref} onClick={toggle}>打开</button>} /></div>)
  const user = userEvent.setup()
  await user.click(screen.getByRole('button', { name: '打开' }))
  expect(screen.getByRole('menu').parentElement).toBe(container.querySelector('.zm'))
  await user.click(screen.getByRole('menuitem', { name: '改名' }))
  expect(selected).toHaveBeenCalledOnce()
  expect(screen.queryByRole('menu')).toBeNull()
  await user.click(screen.getByRole('button', { name: '打开' }))
  await user.keyboard('{Escape}')
  expect(screen.queryByRole('menu')).toBeNull()
})

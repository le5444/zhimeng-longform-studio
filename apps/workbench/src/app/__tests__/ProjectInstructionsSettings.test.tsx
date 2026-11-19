import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { ProjectInstructions } from '../settings/Settings'
import { makeFile, makeSnapshot } from './fixtures'

afterEach(cleanup)

it('lists every supported root instruction source in runtime order, including existing notes', () => {
  const onOpen = vi.fn()
  render(<ProjectInstructions snapshot={makeSnapshot({ files: [makeFile('写作笔记.md'), makeFile('src/AGENTS.md'), makeFile('CLAUDE.md'), makeFile('AGENTS.md')] })} onOpen={onOpen} />)
  expect(screen.getAllByRole('button').map((button) => button.textContent)).toEqual(['打开 AGENTS.md', '打开 CLAUDE.md', '打开 写作笔记.md'])
  fireEvent.click(screen.getByRole('button', { name: '打开 写作笔记.md' }))
  expect(onOpen).toHaveBeenCalledWith('写作笔记.md')
  expect(screen.queryByText(/未找到项目说明文件/)).toBeNull()
})

it('an empty project explains the file location without promising automatic creation', () => {
  render(<ProjectInstructions snapshot={makeSnapshot({ files: [] })} onOpen={vi.fn()} />)
  expect(screen.getByText(/可在项目根目录创建 AGENTS.md 或 CLAUDE.md/)).toBeInTheDocument()
  expect(screen.queryByText(/让 Agent 记.*就会建/)).toBeNull()
})

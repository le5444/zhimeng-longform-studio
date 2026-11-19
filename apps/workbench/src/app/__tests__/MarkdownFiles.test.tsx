import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Markdown } from '../chat/Markdown'
import { UserMessage } from '../chat/Message'
import { resolveFileNavigation } from '../lib/fileNavigation'
import { makeFile, makeSnapshot } from './fixtures'

afterEach(cleanup)

describe('普通文件链接', () => {
  it('裸文件名加冒号行号可打开，网页和危险协议仍不作本地文件', () => {
    const open = vi.fn()
    render(<Markdown text={'[配置](config.json:4) [代码](app.py:2:1) [说明](README:3) [官网](https://example.com/app.py:2) [邮件](mailto:4) [脚本](javascript:4) [数据](data:4)'} onOpenFile={open} />)
    for (const name of ['配置', '代码', '说明']) fireEvent.click(screen.getByRole('button', { name }))
    expect(open.mock.calls.map(([path]) => path)).toEqual(['config.json:4', 'app.py:2:1', 'README:3'])
    for (const name of ['官网', '邮件', '脚本', '数据']) expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    expect(screen.getByRole('link', { name: '官网' })).toHaveAttribute('href', 'https://example.com/app.py:2')
  })

  it('Python、配置、表格、无后缀文件和带行号链接都在工作区打开', () => {
    const open = vi.fn()
    render(<Markdown text={'[代码](src/main.py:12) [配置](config.json#L4) [表格](data/result.csv) [说明](README)'} onOpenFile={open} />)
    for (const name of ['代码', '配置', '表格', '说明']) fireEvent.click(screen.getByRole('button', { name }))
    expect(open.mock.calls.map(([path]) => path)).toEqual(['src/main.py:12', 'config.json#L4', 'data/result.csv', 'README'])
  })

  it('网址即使以md结尾也保留外链；file协议和Windows绝对路径通过内部打开', () => {
    const open = vi.fn()
    render(<Markdown text={'[官网](https://example.com/README.md) [本地](file:///C:/work/main.py) [绝对路径](C:/work/config.json)'} onOpenFile={open} />)
    expect(screen.getByRole('link', { name: '官网' })).toHaveAttribute('href', 'https://example.com/README.md')
    fireEvent.click(screen.getByRole('button', { name: '本地' }))
    fireEvent.click(screen.getByRole('button', { name: '绝对路径' }))
    expect(open.mock.calls.map(([path]) => path)).toEqual(['C:/work/main.py', 'C:/work/config.json'])
  })

  it('引号里的空格/括号路径在AI和用户消息都能点击，原文不被截短', () => {
    const open = vi.fn()
    const text = '看看 @"data/sales (2026).csv" 和 @src/app.py。'
    const view = render(<Markdown text={text} onOpenFile={open} />)
    fireEvent.click(screen.getByRole('button', { name: '@"data/sales (2026).csv"' }))
    expect(decodeURIComponent(open.mock.lastCall![0])).toBe('data/sales (2026).csv')
    view.unmount()
    render(<UserMessage message={{ content: text } as never} isLatest actions={{ onCopy: vi.fn() }} onOpenFile={open} />)
    fireEvent.click(screen.getByRole('button', { name: '@src/app.py' }))
    const snapshot = makeSnapshot({ files: [makeFile('src/app.py')] })
    expect(resolveFileNavigation(snapshot, open.mock.lastCall![0])?.relativePath).toBe('src/app.py')
    expect(screen.getByRole('button', { name: '@"data/sales (2026).csv"' })).toBeInTheDocument()
  })

  it('代码、既有Markdown链接和邮箱里的内容保持原样，损坏的百分号路径不使界面崩溃', () => {
    const source = '`@main.py`\n\n~~~py\n@main.py\n~~~\n\n[@main.py](src/main.py)\n\nname@example.com'
    const open = vi.fn()
    const view = render(<Markdown text={source} onOpenFile={open} />)
    expect([...view.container.querySelectorAll('code')].map((node) => node.textContent)).toEqual(['@main.py', '@main.py\n'])
    expect(screen.getAllByRole('button', { name: '@main.py' })).toHaveLength(1)
    fireEvent.click(screen.getByRole('button', { name: '@main.py' }))
    expect(open).toHaveBeenCalledWith('src/main.py')
    expect(screen.getByText('name@example.com')).toBeInTheDocument()
    view.unmount()
    render(<Markdown text={'[数据](data/100%report.csv) [危险](javascript:alert)'} onOpenFile={open} />)
    fireEvent.click(screen.getByRole('button', { name: '数据' }))
    expect(open).toHaveBeenCalledWith('data/100%report.csv')
    expect(screen.queryByRole('button', { name: '危险' })).not.toBeInTheDocument()
    expect(screen.getByText('危险').closest('a')).not.toHaveAttribute('href', 'javascript:alert')
  })

  it('切项目后即使回答文字相同，也使用新的文件打开函数', () => {
    const before = vi.fn(), after = vi.fn()
    const view = render(<Markdown text={'[代码](main.py)'} onOpenFile={before} />)
    view.rerender(<Markdown text={'[代码](main.py)'} onOpenFile={after} />)
    fireEvent.click(screen.getByRole('button', { name: '代码' }))
    expect(after).toHaveBeenCalledWith('main.py')
    expect(before).not.toHaveBeenCalled()
  })

  it('Markdown章节链接打开真实文件，编码井号和实际带井号文件名保持字面含义', () => {
    const snapshot = makeSnapshot({ files: [makeFile('guide.md'), makeFile('guide#draft.md'), makeFile('guide.md#draft')] })
    const open = vi.fn()
    render(<Markdown text={'[章节](guide.md#usage) [编码文件](guide%23draft.md) [完整文件](guide.md#draft)'}
      onOpenFile={(path) => open(resolveFileNavigation(snapshot, path)?.relativePath)} />)
    for (const name of ['章节', '编码文件', '完整文件']) fireEvent.click(screen.getByRole('button', { name }))
    expect(open.mock.calls.map(([path]) => path)).toEqual(['guide.md', 'guide#draft.md', 'guide.md#draft'])
  })
})

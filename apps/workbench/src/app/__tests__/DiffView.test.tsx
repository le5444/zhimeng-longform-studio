import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'
import { DiffView } from '../components/DiffView'

afterEach(cleanup)

describe('文件差异阅读', () => {
  it('只折叠大片相同行，展开后仍能读到所有原文，内容变化不沿用旧展开状态', () => {
    const original = Array.from({ length: 100 }, (_, i) => `const field${i} = ${i};`)
    const changed = [...original]
    changed[50] = 'const field50 = 42;'
    const { container, rerender } = render(<DiffView before={original.join('\n')} after={changed.join('\n')} />)
    expect(container.querySelectorAll('.zm-diff-line')).toHaveLength(14)
    expect(screen.getByText('const field50 = 50;')).toBeVisible()
    expect(screen.getByText('const field50 = 42;')).toBeVisible()
    const expand = screen.getByRole('button', { name: '展开 44 行未改动内容' })
    expect(expand).toHaveAttribute('aria-expanded', 'false')
    fireEvent.click(expand)
    expect(screen.getByText('const field25 = 25;')).toBeVisible()
    expect(expand).toHaveAttribute('aria-expanded', 'true')
    fireEvent.click(screen.getByRole('button', { name: '展开 43 行未改动内容' }))
    const text = (selector: string) => Array.from(container.querySelectorAll(selector)).map((node) => node.textContent).join('\n')
    expect(text('.zm-diff-line:not(.is-add) .zm-diff-text')).toBe(original.join('\n'))
    expect(text('.zm-diff-line:not(.is-del) .zm-diff-text')).toBe(changed.join('\n'))
    changed[50] = 'const field50 = 43;'
    rerender(<DiffView before={original.join('\n')} after={changed.join('\n')} />)
    expect(container.querySelectorAll('.zm-diff-line')).toHaveLength(14)
    expect(screen.getByRole('button', { name: '展开 44 行未改动内容' })).toHaveAttribute('aria-expanded', 'false')
  })

  it('长行、制表符、缩进和空白差异完整呈现', () => {
    const before = '\t' + 'x'.repeat(200) + '    return value;  '
    const after = '\t' + 'x'.repeat(200) + ' return value;  '
    const { container } = render(<DiffView before={before} after={after} />)
    expect(container.querySelector('.is-del .zm-diff-text')?.textContent).toBe(before)
    expect(container.querySelector('.is-add .zm-diff-text')?.textContent).toBe(after)
  })
})

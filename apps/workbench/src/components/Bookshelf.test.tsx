import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Bookshelf } from './Bookshelf'
import type { ProjectBinding } from '../api/projects'

afterEach(cleanup)

function binding(overrides: Partial<ProjectBinding> = {}): ProjectBinding {
  return {
    project_id: 'project-0123456789abcdef01234567',
    display_name: '正常作品',
    root_path: 'C:\\Novels\\正常作品',
    revision: 1,
    fingerprint: 'a'.repeat(64),
    snapshot_ready: true,
    created_at: '2026-07-27T00:00:00Z',
    updated_at: '2026-07-27T00:00:00Z',
    ...overrides,
  }
}

describe('Bookshelf', () => {
  it('shows an honest fallback instead of crashing when legacy binding names and paths are empty', () => {
    render(
      <Bookshelf
        bindings={[binding({ display_name: '', root_path: '' })]}
        loading={false}
        error={null}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getAllByText('未命名作品').length).toBeGreaterThan(0)
    expect(screen.getByText('作品路径信息缺失，请重新加入书架')).toBeInTheDocument()
  })

  it('uses the folder name when display_name is blank', () => {
    render(
      <Bookshelf
        bindings={[binding({ display_name: '  ', root_path: 'C:\\Novels\\最后一盏灯\\' })]}
        loading={false}
        error={null}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getAllByText('最后一盏灯').length).toBeGreaterThan(0)
  })

  it('keeps validation folders out of the default bookshelf', () => {
    render(
      <Bookshelf
        bindings={[
          binding({ display_name: '《替全宗挡劫十年，我来收债了》创作项目' }),
          binding({
            project_id: 'project-1123456789abcdef01234567',
            display_name: '链路验证_都市长章_外卖雨夜_创作项目',
          }),
        ]}
        loading={false}
        error={null}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    expect(screen.getAllByText('《替全宗挡劫十年，我来收债了》').length).toBeGreaterThan(0)
    expect(screen.queryByText('链路验证_都市长章_外卖雨夜_创作项目')).not.toBeInTheDocument()

    fireEvent.click(screen.getByRole('tab', { name: '开发项目 1' }))
    expect(screen.getAllByText('链路验证_都市长章_外卖雨夜').length).toBeGreaterThan(0)
    expect(screen.queryByText('《替全宗挡劫十年，我来收债了》')).not.toBeInTheDocument()
  })

  it('filters books by their cleaned title', () => {
    render(
      <Bookshelf
        bindings={[
          binding({ display_name: '《拜托，谁家系统天天逼人去死啊》创作草案' }),
          binding({
            project_id: 'project-2123456789abcdef01234567',
            display_name: '宗门只剩七个人，还全是江湖骗子',
          }),
        ]}
        loading={false}
        error={null}
        onOpen={vi.fn()}
        onAdd={vi.fn()}
        onRefresh={vi.fn()}
      />,
    )

    fireEvent.change(screen.getByRole('searchbox', { name: '搜索书名' }), {
      target: { value: '宗门' },
    })
    expect(screen.getAllByText('宗门只剩七个人，还全是江湖骗子').length).toBeGreaterThan(0)
    expect(screen.queryByText('《拜托，谁家系统天天逼人去死啊》')).not.toBeInTheDocument()
  })

  it('imports a selected txt file instead of asking for a machine path', async () => {
    const onAdd = vi.fn().mockResolvedValue(undefined)
    render(
      <Bookshelf
        bindings={[]}
        loading={false}
        error={null}
        onOpen={vi.fn()}
        onAdd={onAdd}
        onRefresh={vi.fn()}
      />,
    )
    const file = new File(['第一章\n正文'], '新作品.txt', { type: 'text/plain' })

    fireEvent.change(screen.getByLabelText('选择 TXT 作品'), {
      target: { files: [file] },
    })

    await waitFor(() => expect(onAdd).toHaveBeenCalledWith(file))
    expect(screen.queryByText(/C:\\Users/)).not.toBeInTheDocument()
  })
})

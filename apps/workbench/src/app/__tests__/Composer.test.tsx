import { useState } from 'react'
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, describe, expect, it, vi } from 'vitest'
import { Composer, type ComposerProps } from '../chat/Composer'
import { makeFile } from './fixtures'
import { ModelControls } from '../chat/models'
import { T } from '../strings'

afterEach(cleanup)

function Harness({ initial = '请帮我检查这份文件', ...over }: Partial<ComposerProps> & { initial?: string }) {
  const [draft, onDraft] = useState(initial)
  return <Composer draft={draft} onDraft={onDraft} onSubmit={vi.fn()} onStop={vi.fn()}
    running={false} busy={false} available inputDisabled={false} placeholder="输入任务"
    files={[makeFile('notes.txt')]} skills={['资料整理', '表格检查']} attachedSkills={[]}
    onAttachSkill={vi.fn()} onDetachSkill={vi.fn()} onCommand={vi.fn()} queuePanel={null}
    queueBusy={false} lastUserText={null} accessFull onToggleAccess={vi.fn()}
    folderControl={null} modelControls={null} textareaRef={null} {...over} />
}

async function openSkills(user: ReturnType<typeof userEvent.setup>) {
  await user.click(screen.getByRole('button', { name: '选择技能' }))
}

describe('输入区可见工具入口', () => {
  it.each(['/help', '检查 @notes'])('Escape dismisses suggestions without deleting the draft: %s', async text => {
    const user = userEvent.setup()
    render(<Harness initial="" />)
    const input = screen.getByRole('textbox', { name: '输入任务' })
    await user.type(input, text)
    expect(screen.getByRole('listbox')).toBeInTheDocument()
    await user.keyboard('{Escape}')
    expect(input).toHaveValue(text)
    expect(screen.queryByRole('listbox')).toBeNull()
    await user.keyboard('{Backspace}')
    expect(screen.getByRole('listbox')).toBeInTheDocument()
  })

  it('reflows the same draft when a panel changes its width or settings are closed', () => {
    let notifyResize!: () => void
    const disconnect = vi.fn()
    vi.stubGlobal('ResizeObserver', class {
      constructor(callback: () => void) { notifyResize = callback }
      observe() {}
      disconnect = disconnect
    })
    try {
      const view = render(<Harness />)
      const input = screen.getByRole('textbox', { name: '输入任务' }) as HTMLTextAreaElement
      let width = 800, height = 100
      Object.defineProperties(input, { clientWidth: { get: () => width }, scrollHeight: { get: () => height } })
      act(() => notifyResize())
      expect(input.style.height).toBe('100px')
      width = 480; height = 180
      act(() => notifyResize())
      expect(input.style.height).toBe('180px')
      width = 0; height = 0
      act(() => notifyResize())
      expect(input.style.height).toBe('180px')
      width = 480; height = 160
      act(() => notifyResize())
      expect(input.style.height).toBe('160px')
      expect(input).toHaveValue('请帮我检查这份文件')
      view.unmount()
      expect(disconnect).toHaveBeenCalledOnce()
    } finally { vi.unstubAllGlobals() }
  })

  it('忙碌收尾期间按钮和回车都排队，队列提交中两者都不可重复提交', () => {
    const submit = vi.fn()
    const view = render(<Harness busy onSubmit={submit} />)
    fireEvent.click(screen.getByRole('button', { name: '排队发送' }))
    fireEvent.keyDown(screen.getByRole('textbox', { name: '输入任务' }), { key: 'Enter' })
    expect(submit).toHaveBeenCalledTimes(2)
    view.rerender(<Harness busy queueBusy onSubmit={submit} />)
    expect(screen.getByRole('button', { name: '排队发送' })).toBeDisabled()
    fireEvent.keyDown(screen.getByRole('textbox', { name: '输入任务' }), { key: 'Enter' })
    expect(submit).toHaveBeenCalledTimes(2)
  })

  it('两万个文件按页显示，翻页和搜索仍能引用尾部文件', () => {
    const files = Array.from({ length: 20_000 }, (_, i) => makeFile(`data/report-${String(i).padStart(5, '0')}.txt`))
    const view = render(<Harness files={files} />)
    const input = screen.getByRole('textbox', { name: '输入任务' }) as HTMLTextAreaElement
    input.setSelectionRange(input.value.length, input.value.length)
    fireEvent.click(screen.getByRole('button', { name: '引用文件' }))
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getAllByRole('button').length).toBeLessThan(50)
    expect(within(dialog).getByRole('status')).toHaveTextContent('1-40 / 20000')
    fireEvent.click(within(dialog).getByRole('button', { name: '下一页文件' }))
    expect(within(dialog).getByRole('status')).toHaveTextContent('41-80 / 20000')
    fireEvent.change(within(dialog).getByRole('textbox'), { target: { value: 'report-19999' } })
    expect(within(dialog).queryByRole('status')).toBeNull()
    fireEvent.click(within(dialog).getByRole('button', { name: /report-19999/ }))
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件 @data/report-19999.txt ')
    view.unmount()
  })

  it('电脑粘贴图片和拖入图片都交给真实附件入口，普通粘贴仍按文字处理', () => {
    const add = vi.fn()
    const { container } = render(<Harness onImages={add} />)
    const picture = new File(['png'], 'clipboard.png', { type: 'image/png' })
    fireEvent.paste(screen.getByRole('textbox', { name: '输入任务' }), { clipboardData: { items: [{ kind: 'file', getAsFile: () => picture }], files: [picture] } })
    expect(add).toHaveBeenCalledWith([picture])
    fireEvent.drop(container.querySelector('.zm-compose-surface')!, { dataTransfer: { files: [picture] } })
    expect(add).toHaveBeenCalledTimes(2)
    fireEvent.paste(screen.getByRole('textbox', { name: '输入任务' }), { clipboardData: { items: [], files: [] } })
    expect(add).toHaveBeenCalledTimes(2)
  })

  it('图片-only也可发送，上传中不可发，失败可重试和移除，选图入口保留', async () => {
    const submit = vi.fn(), remove = vi.fn(), retry = vi.fn(), add = vi.fn()
    const images = [{ clientId: 'local', name: 'sample.png', status: 'failed' as const, error: '图片上传失败' }]
    const view = render(<Harness initial="" images={images} imagesBlocked onSubmit={submit} onImages={add} onRemoveImage={remove} onRetryImage={retry} />)
    expect(screen.getByRole('button', { name: '发送' })).toBeDisabled()
    fireEvent.click(screen.getByRole('button', { name: '重试上传' }))
    expect(retry).toHaveBeenCalledWith('local')
    fireEvent.click(screen.getByRole('button', { name: '移除图片：sample.png' }))
    expect(remove).toHaveBeenCalledWith('local')
    view.rerender(<Harness initial="" images={[{ ...images[0], status: 'ready' }]} imagesBlocked={false} onSubmit={submit} onImages={add} />)
    fireEvent.click(screen.getByRole('button', { name: '发送' }))
    expect(submit).toHaveBeenCalledOnce()
    const picker = vi.spyOn(screen.getByLabelText('选择图片文件'), 'click')
    await userEvent.setup().click(screen.getByRole('button', { name: '添加图片' }))
    expect(picker).toHaveBeenCalledOnce()
    const picture = new File(['png'], 'picked.png', { type: 'image/png' })
    fireEvent.change(screen.getByLabelText('选择图片文件'), { target: { files: [picture] } })
    expect(add).toHaveBeenCalledWith([picture])
  })

  it('权限、模型、思考强度和容量直接可见，附件各有直接入口', async () => {
    const user = userEvent.setup()
    const toggle = vi.fn()
    const model = 'grok-4.6-very-long-model-name-with-complete-version-20261011'
    render(<div className="zm"><Harness onToggleAccess={toggle} modelControls={<ModelControls provider={{ settings: { configured: true, model, reasoning_effort: 'high', context_window: '256k' } } as never}
      disabled={false} onProviderChanged={vi.fn()} onOpenSettings={vi.fn()} notify={vi.fn()} />} /></div>)
    expect(screen.getByRole('combobox', { name: T.agent.modelPick })).toHaveValue(model)
    expect(screen.getByRole('combobox', { name: T.agent.effortPick })).toHaveValue('high')
    expect(screen.getByRole('combobox', { name: T.agent.contextWindowPick })).toHaveValue('256k')
    expect(screen.getByRole('combobox', { name: T.agent.modelPick }).closest('label')).toHaveAttribute('title', model)
    expect(screen.getByRole('button', { name: '完全访问' })).toHaveAttribute('aria-pressed', 'true')
    expect(screen.getByRole('button', { name: '选择技能' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '引用文件' })).toBeEnabled()
    expect(screen.getByRole('button', { name: '添加图片' })).toBeEnabled()
    await user.click(screen.getByRole('button', { name: '完全访问' }))
    expect(toggle).toHaveBeenCalledOnce()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('文件选择用portal且点击外部收起，输入法确认不触发发送', async () => {
    const submit = vi.fn()
    const user = userEvent.setup()
    const view = render(<Harness onSubmit={submit} />)
    const input = screen.getByRole('textbox', { name: '输入任务' })
    fireEvent.keyDown(input, { key: 'Enter', code: 'Enter', isComposing: true })
    expect(submit).not.toHaveBeenCalled()
    await user.click(screen.getByRole('button', { name: '引用文件' }))
    expect(screen.getByRole('dialog').parentElement).toBe(document.body)
    expect(view.container.querySelector('[role="dialog"]')).toBeNull()
    await waitFor(() => expect(screen.getByRole('textbox', { name: '搜索项目文件' })).toHaveFocus())
    fireEvent.pointerDown(document.body)
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(input).toHaveValue('请帮我检查这份文件')
  })

  it('新增文件引用保留光标前后文本和引号路径，不先改成孤立@', async () => {
    const user = userEvent.setup()
    render(<Harness initial="检查 以及结尾" files={[makeFile('data/sales (2026).csv')]} />)
    const input = screen.getByRole('textbox', { name: '输入任务' }) as HTMLTextAreaElement
    input.focus(); input.setSelectionRange(3, 3)
    await user.click(screen.getByRole('button', { name: '引用文件' }))
    await user.click(screen.getByRole('button', { name: /sales \(2026\)\.csv/ }))
    await waitFor(() => expect(input).toHaveFocus())
    expect(input).toHaveValue('检查 @"data/sales (2026).csv" 以及结尾')
  })

  it('运行中仍可直接看到权限但不能中途切换，Escape不停止任务', async () => {
    const stop = vi.fn(), toggle = vi.fn()
    const user = userEvent.setup()
    render(<Harness running onStop={stop} onToggleAccess={toggle} />)
    expect(screen.getByRole('button', { name: '完全访问' })).toBeDisabled()
    await openSkills(user)
    await user.keyboard('{Escape}')
    expect(stop).not.toHaveBeenCalled()
    expect(toggle).not.toHaveBeenCalled()
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件')
  })

  it('选择带空格和括号的表格文件会插入完整引号引用', async () => {
    const user = userEvent.setup()
    render(<Harness initial="" files={[makeFile('data/sales (2026).csv')]} />)
    await user.type(screen.getByRole('textbox', { name: '输入任务' }), '@sales')
    await user.click(await screen.findByRole('option', { name: /sales \(2026\)\.csv/ }))
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('@"data/sales (2026).csv" ')
  })

  it('运行时排队与本轮引导是两个独立动作，更新队列时不能重复发送', async () => {
    const user = userEvent.setup()
    const queue = vi.fn(), steer = vi.fn(), stop = vi.fn()
    const view = render(<Harness running onSubmit={queue} onSteer={steer} onStop={stop} />)
    await user.click(screen.getByRole('button', { name: '引导这一轮' }))
    expect(steer).toHaveBeenCalledOnce()
    expect(queue).not.toHaveBeenCalled()
    expect(stop).not.toHaveBeenCalled()
    view.rerender(<Harness running queueBusy onSubmit={queue} onSteer={steer} onStop={stop} />)
    expect(screen.getByRole('button', { name: '引导这一轮' })).toBeDisabled()
    expect(screen.getByRole('button', { name: '排队发送' })).toBeDisabled()
  })

  it('选择技能保留输入草稿，按名称筛选后只附加技能', async () => {
    const user = userEvent.setup()
    const attach = vi.fn()
    render(<Harness onAttachSkill={attach} />)
    await openSkills(user)
    await user.type(screen.getByRole('textbox', { name: '搜索可用技能' }), '表格')
    expect(screen.queryByRole('button', { name: '资料整理' })).toBeNull()
    await user.click(screen.getByRole('button', { name: '表格检查' }))
    expect(attach).toHaveBeenCalledWith('表格检查')
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件')
    expect(screen.queryByRole('dialog', { name: '选择技能' })).toBeNull()
  })

  it('Escape关闭技能选择并返回输入框，原文完整保留', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await openSkills(user)
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).toBeNull()
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveFocus()
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件')
  })

  it('文件引用从光标位置插入，不丢掉其余输入', async () => {
    const user = userEvent.setup()
    render(<Harness initial="检查 文档" />)
    const input = screen.getByRole('textbox', { name: '输入任务' }) as HTMLTextAreaElement
    input.focus()
    input.setSelectionRange(3, 3)
    await user.click(screen.getByRole('button', { name: '引用文件' }))
    await user.click(await screen.findByRole('button', { name: 'notes.txt' }))
    expect(input).toHaveValue('检查 @notes.txt 文档')
  })

  it('没有技能时可进入技能库；没有项目时引用入口清楚不可用', async () => {
    const user = userEvent.setup()
    const open = vi.fn()
    render(<Harness files={null} skills={[]} onOpenSkills={open} />)
    await user.click(screen.getByRole('button', { name: '引用文件' }))
    expect(screen.getByText('打开文件夹后，可以引用其中的文件。')).toBeInTheDocument()
    await openSkills(user)
    await user.click(screen.getByRole('button', { name: '打开技能库' }))
    expect(open).toHaveBeenCalledOnce()
    expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件')
  })

  it('点击输入框关闭手动技能面板，仍能直接输入', async () => {
    const user = userEvent.setup()
    render(<Harness />)
    await openSkills(user)
    fireEvent.focus(screen.getByRole('textbox', { name: '输入任务' }))
    expect(screen.queryByRole('dialog')).toBeNull()
  })
})

it('无项目时引用文件能就地打开文件夹，保留尚未发送的要求', async () => {
  const open = vi.fn(), submit = vi.fn()
  render(<Harness files={null} onOpenFolder={open} onSubmit={submit} />)
  await userEvent.setup().click(screen.getByRole('button', { name: '引用文件' }))
  await userEvent.setup().click(within(screen.getByRole('dialog')).getByRole('button', { name: '打开文件夹' }))
  expect(open).toHaveBeenCalledOnce()
  expect(submit).not.toHaveBeenCalled()
  expect(screen.getByRole('textbox', { name: '输入任务' })).toHaveValue('请帮我检查这份文件')
})

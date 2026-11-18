/**
 * 「设置 → 技能库」（9-28；9-29 回炉重造后是一张列表、每行一个开关）：本机别家 agent 的技能文件夹一家一个开关，别家的技能能一键复制进织梦。
 * 作者：「这个应用有一个技能库，还可以其他的智能库，可以一键探索使用其他的」。
 */
import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import type { UserSkillsView } from '../../api/userSkills'
import { SkillsTab } from '../settings/SkillsSection'
import { S as L } from '../settings/settingsText'
import { T } from '../strings'
import { SettingsDraftContext } from '../settings/drafts'
import type { ConfirmRequest } from '../ui'

const api = vi.hoisted(() => ({
  readUserSkills: vi.fn(),
  setUserSkillEnabled: vi.fn(),
  createUserSkill: vi.fn(),
  readUserSkillInstructions: vi.fn(),
  saveUserSkillInstructions: vi.fn(),
  setSkillSourceEnabled: vi.fn(),
  copyUserSkillToLibrary: vi.fn(),
}))

vi.mock('../../api/userSkills', () => api)

const S = T.settings.skills

function view(over: Partial<UserSkillsView> = {}): UserSkillsView {
  return {
    schema: 'zhimeng.user-skills.v1',
    skills_root: 'C:/app/skills',
    roots: [
      { path: 'C:/app/skills', origin: 'project', exists: true, source: 'zhimeng', label: '织梦' },
      { path: 'C:/home/.codex/skills', origin: 'external', exists: true, source: 'codex', label: 'Codex' },
    ],
    sources: [
      { key: 'codex', label: 'Codex', path: 'C:/home/.codex/skills', exists: true, count: 17, enabled: true, default_enabled: true },
      { key: 'workbuddy', label: 'WorkBuddy', path: 'C:/home/.workbuddy/skills', exists: true, count: 25, enabled: false, default_enabled: false },
      { key: 'zcode', label: 'ZCode', path: 'C:/home/.zcode/skills', exists: false, count: 0, enabled: false, default_enabled: false },
    ],
    exists: true,
    total: 2,
    enabled: 2,
    skills: [
      { name: '小说写法', description: '写长篇时翻的经验。', folder: 'C:/app/skills/novel', has_scripts: false, enabled: true, origin: 'project', source: 'zhimeng', source_label: '织梦' },
      { name: 'pptx', description: 'Use this skill for .pptx files.', folder: 'C:/home/.codex/skills/pptx', has_scripts: false, enabled: true, origin: 'external', source: 'codex', source_label: 'Codex' },
    ],
    ...over,
  }
}

function renderTab(notify = vi.fn()) {
  render(<SkillsTab notify={notify} askConfirm={vi.fn()} />)
  return notify
}

beforeEach(() => {
  vi.clearAllMocks()
})

afterEach(cleanup)

describe('技能库', () => {
  it('填好真正使用方法才创建，并将正文一次保存给后端', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const body = '# 周报整理\n\n读取本周记录，保留来源，按完成情况分类。\n'
    api.createUserSkill.mockResolvedValue({ ok: true, message: 'ok', skills: view() })
    const notify = renderTab()
    fireEvent.click(await screen.findByRole('button', { name: S.add }))
    fireEvent.change(screen.getByRole('textbox', { name: S.newName }), { target: { value: '周报整理' } })
    fireEvent.change(screen.getByRole('textbox', { name: S.newDesc }), { target: { value: '整理工作记录' } })
    const create = screen.getByRole('button', { name: S.createIt })
    expect(create).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: /^使用方法/ }), { target: { value: ' \n ' } })
    expect(create).toBeDisabled()
    fireEvent.change(screen.getByRole('textbox', { name: /^使用方法/ }), { target: { value: body } })
    fireEvent.click(create)
    await waitFor(() => expect(api.createUserSkill).toHaveBeenCalledWith('周报整理', '整理工作记录', body))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', S.created('周报整理')))
    expect(screen.queryByRole('textbox', { name: /^使用方法/ })).toBeNull()
    expect(api.readUserSkillInstructions).not.toHaveBeenCalled()
    expect(api.saveUserSkillInstructions).not.toHaveBeenCalled()
  })

  it('创建失败保留名字、用途和正文，重试仍发送同一份方法', async () => {
    api.readUserSkills.mockResolvedValue(view())
    api.createUserSkill.mockRejectedValueOnce(new Error('同名技能已经存在')).mockResolvedValueOnce({ ok: true, message: 'ok', skills: view() })
    const notify = renderTab()
    fireEvent.click(await screen.findByRole('button', { name: S.add }))
    fireEvent.change(screen.getByRole('textbox', { name: S.newName }), { target: { value: '周报整理' } })
    fireEvent.change(screen.getByRole('textbox', { name: S.newDesc }), { target: { value: '整理工作记录' } })
    fireEvent.change(screen.getByRole('textbox', { name: /^使用方法/ }), { target: { value: '保留来源。' } })
    fireEvent.click(screen.getByRole('button', { name: S.createIt }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('danger', expect.stringContaining('同名技能已经存在')))
    expect(screen.getByRole('textbox', { name: S.newName })).toHaveValue('周报整理')
    expect(screen.getByRole('textbox', { name: S.newDesc })).toHaveValue('整理工作记录')
    expect(screen.getByRole('textbox', { name: /^使用方法/ })).toHaveValue('保留来源。')
    fireEvent.change(screen.getByRole('textbox', { name: S.newName }), { target: { value: '周报整理2' } })
    fireEvent.click(screen.getByRole('button', { name: S.createIt }))
    await waitFor(() => expect(api.createUserSkill).toHaveBeenLastCalledWith('周报整理2', '整理工作记录', '保留来源。'))
  })

  it('只写正文也参与离开提醒，收起新建不丢内容，取消沿用草稿放弃确认', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const updateDraft = vi.fn()
    const askConfirm = vi.fn()
    render(<SettingsDraftContext.Provider value={updateDraft}><SkillsTab notify={vi.fn()} askConfirm={askConfirm} /></SettingsDraftContext.Provider>)
    fireEvent.click(await screen.findByRole('button', { name: S.add }))
    fireEvent.change(screen.getByRole('textbox', { name: /^使用方法/ }), { target: { value: '保留这份未保存方法' } })
    expect(updateDraft).toHaveBeenLastCalledWith('skills', true)
    fireEvent.click(screen.getByRole('button', { name: S.add }))
    fireEvent.click(screen.getByRole('button', { name: S.add }))
    expect(screen.getByRole('textbox', { name: /^使用方法/ })).toHaveValue('保留这份未保存方法')
    fireEvent.click(screen.getByRole('button', { name: T.common.cancel }))
    expect(askConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '新技能尚未保存' }))
    expect(screen.getByRole('textbox', { name: /^使用方法/ })).toHaveValue('保留这份未保存方法')
    await act(async () => { await (askConfirm.mock.calls[0][0] as ConfirmRequest).onConfirm() })
    expect(updateDraft).toHaveBeenLastCalledWith('skills', false)
    fireEvent.click(screen.getByRole('button', { name: S.add }))
    expect(screen.getByRole('textbox', { name: /^使用方法/ })).toHaveValue('')
    expect(api.createUserSkill).not.toHaveBeenCalled()
  })

  it('已有技能仍能编辑全文，返回时保护未保存修改', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const content = '---\nname: 小说写法\ndescription: 已有技能\n---\n\n已有正文'
    api.readUserSkillInstructions.mockResolvedValue({ name: '小说写法', content, editable: true })
    api.saveUserSkillInstructions.mockResolvedValue({ ok: true })
    const askConfirm = vi.fn()
    render(<SkillsTab notify={vi.fn()} askConfirm={askConfirm} />)
    fireEvent.click(await screen.findByRole('button', { name: S.edit }))
    await screen.findByRole('heading', { name: S.editTitle('小说写法') })
    const editor = screen.getByRole('textbox')
    fireEvent.change(editor, { target: { value: `${content}\n新补充的方法` } })
    fireEvent.click(screen.getByRole('button', { name: T.common.back }))
    expect(askConfirm).toHaveBeenCalledWith(expect.objectContaining({ title: '技能内容尚未保存' }))
    expect(editor).toHaveValue(`${content}\n新补充的方法`)
    fireEvent.click(screen.getByRole('button', { name: S.save }))
    await waitFor(() => expect(api.saveUserSkillInstructions).toHaveBeenCalledWith('小说写法', `${content}\n新补充的方法`, { expectedSha256: undefined }))
    expect(askConfirm).toHaveBeenCalledTimes(1)
    expect(await screen.findByText('pptx')).toBeInTheDocument()
  })

  it('保存带读取版本，冲突在详情内保留原草稿', async () => {
    api.readUserSkills.mockResolvedValue(view())
    api.readUserSkillInstructions.mockResolvedValue({ name: '小说写法', content: '原内容', editable: true, sha256: 'a'.repeat(64) })
    api.saveUserSkillInstructions.mockRejectedValue(new Error('技能文件已被其他操作修改'))
    render(<SkillsTab notify={vi.fn()} askConfirm={vi.fn()} />)
    fireEvent.click(await screen.findByRole('button', { name: S.edit }))
    const editor = await screen.findByRole('textbox', { name: '技能说明' })
    fireEvent.change(editor, { target: { value: '待合并草稿' } })
    fireEvent.click(screen.getByRole('button', { name: S.save }))
    await waitFor(() => expect(screen.getByRole('alert')).toHaveTextContent('技能文件已被其他操作修改'))
    expect(api.saveUserSkillInstructions).toHaveBeenCalledWith('小说写法', '待合并草稿', { expectedSha256: 'a'.repeat(64) })
    expect(editor).toHaveValue('待合并草稿')
    expect(editor).not.toHaveAttribute('readonly')
  })

  it('外部技能仍只读，创建正文功能不会开放外部写入', async () => {
    api.readUserSkills.mockResolvedValue(view())
    api.readUserSkillInstructions.mockResolvedValue({ name: 'pptx', content: '# 外部技能方法', editable: false })
    renderTab()
    fireEvent.click(await screen.findByRole('button', { name: S.read }))
    expect(await screen.findByDisplayValue('# 外部技能方法')).toHaveAttribute('readonly')
    expect(screen.getByRole('textbox')).toHaveValue('# 外部技能方法')
    expect(screen.queryByRole('button', { name: S.save })).toBeNull()
    fireEvent.click(screen.getByRole('button', { name: T.common.back }))
    expect(await screen.findByRole('button', { name: S.copyIn })).toBeEnabled()
    expect(api.saveUserSkillInstructions).not.toHaveBeenCalled()
  })

  it('刷新期间保留清单，显示忙碌并串行处理来源和技能修改', async () => {
    let finish!: (next: UserSkillsView) => void
    api.readUserSkills.mockResolvedValueOnce(view()).mockImplementationOnce(() => new Promise<UserSkillsView>((resolve) => { finish = resolve }))
    renderTab()
    await screen.findByText('pptx')
    const refresh = screen.getByRole('button', { name: S.refresh })
    fireEvent.click(refresh)
    expect(refresh).toBeDisabled()
    expect(screen.getByText('pptx')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: '技能来源' }))
    expect(screen.getByRole('switch', { name: L.sourceSwitch('WorkBuddy') })).toBeDisabled()
    fireEvent.click(refresh)
    fireEvent.click(screen.getByRole('switch', { name: L.skillSwitch('pptx') }))
    expect(api.readUserSkills).toHaveBeenCalledTimes(2)
    expect(api.setUserSkillEnabled).not.toHaveBeenCalled()
    await act(async () => finish(view()))
    expect(refresh).toBeEnabled()
  })

  it('技能已停用但列表刷新失败时保留成功事实并给出刷新提醒', async () => {
    api.readUserSkills.mockResolvedValueOnce(view()).mockRejectedValueOnce(new Error('列表暂时不可用'))
    api.setUserSkillEnabled.mockResolvedValue({ name: 'pptx', enabled: false })
    const notify = renderTab()
    fireEvent.click(await screen.findByRole('switch', { name: L.skillSwitch('pptx') }))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('warn', expect.stringContaining('列表刷新失败')))
    expect(notify).toHaveBeenCalledWith('warn', expect.stringContaining(S.turnedOff('pptx')))
    expect(notify).not.toHaveBeenCalledWith('ok', expect.anything())
    expect(notify).not.toHaveBeenCalledWith('danger', expect.anything())
    expect(screen.getByText('pptx')).toBeInTheDocument()
  })

  it('离开技能设置后，迟到的刷新错误不再弹出提示', async () => {
    let fail!: (error: Error) => void
    api.readUserSkills.mockImplementationOnce(() => new Promise((_resolve, reject) => { fail = reject }))
    const notify = vi.fn()
    const tab = render(<SkillsTab notify={notify} askConfirm={vi.fn()} />)
    tab.unmount()
    await act(async () => fail(new Error('迟到错误')))
    expect(notify).not.toHaveBeenCalled()
  })

  it('来源切换完成后把后端清单通知聊天，无需额外重读', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const next = view({ skills: [view().skills[0]] })
    api.setSkillSourceEnabled.mockResolvedValue(next)
    const receive = vi.fn()
    window.addEventListener('zhimeng:skills-changed', receive)
    try {
      renderTab()
      await screen.findByText('pptx')
      receive.mockClear()
      fireEvent.click(screen.getByRole('button', { name: '技能来源' }))
    fireEvent.click(screen.getByRole('switch', { name: L.sourceSwitch('Codex') }))
      await waitFor(() => expect(receive).toHaveBeenCalledWith(expect.objectContaining({ detail: { view: next } })))
      expect(api.readUserSkills).toHaveBeenCalledTimes(1)
      expect(screen.queryByText('pptx')).not.toBeInTheDocument()
    } finally {
      window.removeEventListener('zhimeng:skills-changed', receive)
    }
  })

  it('列出这台电脑上别家 agent 的技能文件夹、几个技能、用没用，没装的合成一行', async () => {
    api.readUserSkills.mockResolvedValue(view())
    renderTab()

    expect(await screen.findByText(S.sourcesTitle)).toBeTruthy()
    expect(screen.getByText(S.sourceLine(25, 'C:/home/.workbuddy/skills'))).toBeTruthy()
    expect(screen.getByText(S.missingSources(['ZCode']))).toBeTruthy()
    // 每个技能那一行写明来自哪一家。
    const row = screen.getByText('pptx').closest('li') as HTMLElement
    expect(within(row).getByText('Codex')).toBeTruthy()
  })

  it('点开关就用上那一家的技能，页面照后端返回的刷新', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const after = view({
      sources: view().sources!.map((item) => (item.key === 'workbuddy' ? { ...item, enabled: true } : item)),
    })
    api.setSkillSourceEnabled.mockResolvedValue(after)
    const notify = renderTab()

    await screen.findByText(S.sourcesTitle)
    fireEvent.click(screen.getByRole('button', { name: '技能来源' }))
    const source = screen.getByRole('switch', { name: L.sourceSwitch('WorkBuddy') })
    expect(source).toHaveAttribute('aria-checked', 'false')
    fireEvent.click(source)

    await waitFor(() => expect(api.setSkillSourceEnabled).toHaveBeenCalledWith('workbuddy', true))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', S.sourceTurnedOn('WorkBuddy', 25)))
    await waitFor(() => expect(screen.getByRole('switch', { name: L.sourceSwitch('WorkBuddy') })).toHaveAttribute('aria-checked', 'true'))
  })

  it('别家的技能能一键复制进织梦，复制完就是织梦自己的、能改', async () => {
    api.readUserSkills.mockResolvedValue(view())
    const copied = view({
      skills: view().skills.map((item) =>
        item.name === 'pptx' ? { ...item, origin: 'project', source: 'zhimeng', source_label: '织梦', folder: 'C:/app/skills/pptx' } : item,
      ),
    })
    api.copyUserSkillToLibrary.mockResolvedValue({ ok: true, message: 'ok', skills: copied })
    const notify = renderTab()

    fireEvent.click(await screen.findByRole('button', { name: S.copyIn }))

    await waitFor(() => expect(api.copyUserSkillToLibrary).toHaveBeenCalledWith('pptx'))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', S.copied('pptx')))
    expect(screen.queryByRole('button', { name: S.copyIn })).toBeNull()
    expect(screen.getAllByRole('button', { name: S.edit })).toHaveLength(2)
  })

  it('每个技能一行一个开关：关掉就调后端停用、重读列表', async () => {
    api.readUserSkills.mockResolvedValue(view())
    api.setUserSkillEnabled.mockResolvedValue({ ok: true })
    const notify = renderTab()

    const toggle = await screen.findByRole('switch', { name: L.skillSwitch('小说写法') })
    expect(toggle).toHaveAttribute('aria-checked', 'true')
    api.readUserSkills.mockResolvedValue(view({ skills: view().skills.map((item) => (item.name === '小说写法' ? { ...item, enabled: false } : item)) }))
    fireEvent.click(toggle)

    await waitFor(() => expect(api.setUserSkillEnabled).toHaveBeenCalledWith('小说写法', false))
    await waitFor(() => expect(notify).toHaveBeenCalledWith('ok', S.turnedOff('小说写法')))
    expect(screen.getByRole('switch', { name: L.skillSwitch('小说写法') })).toHaveAttribute('aria-checked', 'false')
    // 停用只影响 Agent 发现，仍能查看和修改说明。
    expect(screen.getAllByRole('button', { name: S.edit })[0]).toBeEnabled()
  })

  it('技能按名称、状态和来源筛选，不修改启用状态', async () => {
    api.readUserSkills.mockResolvedValue(view())
    renderTab()
    await screen.findByText('pptx')
    fireEvent.change(screen.getByRole('textbox', { name: '搜索技能' }), { target: { value: 'ppt' } })
    expect(screen.queryByText('小说写法')).toBeNull()
    fireEvent.change(screen.getByRole('combobox', { name: '技能状态' }), { target: { value: 'off' } })
    expect(screen.getByText('没有匹配的技能')).toBeInTheDocument()
    fireEvent.change(screen.getByRole('combobox', { name: '技能状态' }), { target: { value: 'all' } })
    fireEvent.change(screen.getByRole('combobox', { name: '技能来源筛选' }), { target: { value: 'Codex' } })
    expect(screen.getByText('pptx')).toBeInTheDocument()
    expect(api.setUserSkillEnabled).not.toHaveBeenCalled()
  })
})

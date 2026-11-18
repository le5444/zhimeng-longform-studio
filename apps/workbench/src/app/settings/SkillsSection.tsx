import { SettingsDisclosure } from './navigation'
import { useSettingsPane } from './useSettingsPane'
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { Plus, RefreshCw, Search } from 'lucide-react'
import {
  copyUserSkillToLibrary,
  createUserSkill,
  readUserSkillInstructions,
  readUserSkills,
  saveUserSkillInstructions,
  setSkillSourceEnabled,
  setUserSkillEnabled,
  type SkillSourceView,
  type UserSkillsView,
  type UserSkillView,
} from '../../api/userSkills'
import { authorFacingError } from '../../lib/authorFacingError'
import { T } from '../strings'
import { Switch, type AskConfirm, type Notify } from '../ui'
import { Button, Empty, Field, Input, Panel, Select, Textarea } from './kit'
import { S as L } from './settingsText'
import { useSettingsDraft } from './drafts'
import { publishSkillsChanged } from '../lib/skillsChanged'

/**
 * 技能：技能文件夹里一个带 SKILL.md 的子文件夹就是一个技能。
 *
 * 读哪几个文件夹由后端定（织梦自己的 skills/，加 Codex、通用 .agents、Claude 的共享文件夹），
 * 同名的以排在前面的为准，这里照实列出来，没用上的同名副本也列。
 * 启用的技能名字和用途在每个对话里都给 Agent（挂在读技能的工具说明里），全文它用到才读。
 * 所以停用不只是眼不见——是真的不给它看。
 *
 * 织梦自己文件夹里的技能可以直接在这儿改说明书；共享文件夹里的只能看。
 *
 * 9-28 起是「技能库」：本机别家 agent 的技能文件夹一家一个开关（Codex、通用、Claude 默认开，
 * 别的默认关、点一下就用上，原地读、不复制也不改它们）；别家的技能能一键「复制进织梦」，复制后在这儿能改。
 * 作者：「这个应用有一个技能库，还可以其他的智能库，可以一键探索使用其他的」。
 */
const SKILL_DESTINATIONS = { 'skill-list': 'library', 'skill-sources': 'sources' } as const

export function SkillsTab({ notify, askConfirm }: { notify: Notify; askConfirm: AskConfirm }) {
  const S = T.settings.skills
  const [pane, setPane] = useSettingsPane('library', SKILL_DESTINATIONS)
  const [skills, setSkills] = useState<UserSkillView[]>([])
  const [root, setRoot] = useState('')
  const [sources, setSources] = useState<SkillSourceView[]>([])
  const [state, setState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [busy, setBusy] = useState<string | null>(null)
  const mounted = useRef(true)
  const operation = useRef<symbol | null>(null)
  const [editing, setEditing] = useState<{ name: string; text: string; original: string; editable: boolean; sha256?: string; folder: string; source: string } | null>(null)
  const [saveError, setSaveError] = useState('')
  const [adding, setAdding] = useState(false)
  const [newName, setNewName] = useState('')
  const [newDesc, setNewDesc] = useState('')
  const [newContent, setNewContent] = useState('')
  const [query, setQuery] = useState('')
  const [filter, setFilter] = useState('all')
  const [sourceFilter, setSourceFilter] = useState('all')
  const newDirty = Boolean(newName.trim() || newDesc.trim() || newContent.trim())
  const dirty = newDirty || Boolean(editing?.editable && editing.text !== editing.original)
  useSettingsDraft('skills', dirty)
  const filtered = skills.filter((skill) =>
    `${skill.name} ${skill.description}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase())
    && (filter === 'all' || (filter === 'on' ? skill.enabled : !skill.enabled))
    && (sourceFilter === 'all' || (skill.source_label || '织梦') === sourceFilter))

  const apply = useCallback((view: UserSkillsView) => {
    setSkills(view.skills)
    setRoot(view.skills_root)
    setSources(view.sources ?? [])
    setState('ready')
  }, [])

  const run = useCallback(async (key: string, work: (isCurrent: () => boolean) => Promise<void>, failure: string) => {
    if (!mounted.current || operation.current) return
    const owner = Symbol(key)
    operation.current = owner
    const isCurrent = () => mounted.current && operation.current === owner
    setBusy(key)
    try {
      await work(isCurrent)
    } catch (error) {
      if (isCurrent()) notify('danger', authorFacingError(error, failure))
    } finally {
      if (operation.current === owner) {
        operation.current = null
        if (mounted.current) setBusy(null)
      }
    }
  }, [notify])

  const refresh = useCallback(() => run('refresh', async (isCurrent) => {
    try {
      const view = await readUserSkills()
      if (!isCurrent()) return
      apply(view)
      publishSkillsChanged(view)
    } catch (error) {
      if (isCurrent()) setState('error')
      throw error
    }
  }, S.loadFailed), [S.loadFailed, apply, run])

  useEffect(() => {
    mounted.current = true
    void refresh()
    return () => { mounted.current = false; operation.current = null }
  }, [refresh])

  const changed = async (isCurrent: () => boolean, message: string, snapshot?: UserSkillsView) => {
    try {
      const view = snapshot ?? await readUserSkills()
      // 写入即使在离开设置后才完成，仍让正在使用的聊天更新清单。
      if (!isCurrent()) { publishSkillsChanged(); return }
      publishSkillsChanged(view)
      apply(view)
      notify('ok', message)
    } catch (error) {
      publishSkillsChanged()
      if (!isCurrent()) return
      setState('error')
      notify('warn', `${message}；列表刷新失败，请重试刷新。${authorFacingError(error, S.loadFailed)}`)
    }
  }

  const enabledCount = useMemo(() => skills.filter((item) => item.enabled).length, [skills])
  const installed = useMemo(() => sources.filter((item) => item.exists), [sources])
  const missing = useMemo(() => sources.filter((item) => !item.exists).map((item) => item.label), [sources])

  const toggleSource = (source: SkillSourceView) => run(`source:${source.key}`, async (isCurrent) => {
    const view = await setSkillSourceEnabled(source.key, !source.enabled)
    await changed(isCurrent, source.enabled ? S.sourceTurnedOff(source.label) : S.sourceTurnedOn(source.label, source.count), view)
  }, S.sourceToggleFailed)

  const copyIn = (skill: UserSkillView) => run(skill.name, async (isCurrent) => {
    const body = await copyUserSkillToLibrary(skill.name)
    await changed(isCurrent, S.copied(skill.name), body.skills)
  }, S.copyFailed)

  const toggle = (skill: UserSkillView) => run(skill.name, async (isCurrent) => {
    await setUserSkillEnabled(skill.name, !skill.enabled)
    if (isCurrent()) setSkills((current) => current.map((item) => item.name === skill.name ? { ...item, enabled: !skill.enabled } : item))
    await changed(isCurrent, skill.enabled ? S.turnedOff(skill.name) : S.turnedOn(skill.name))
  }, S.toggleFailed)

  const open = (skill: UserSkillView) => run(skill.name, async (isCurrent) => {
    const body = await readUserSkillInstructions(skill.name)
    if (isCurrent()) {
      setSaveError('')
      setEditing({ name: skill.name, text: body.content, original: body.content, editable: body.editable, sha256: body.sha256, folder: skill.folder, source: skill.source_label || '织梦' })
    }
  }, S.readFailed)

  const save = async () => {
    if (!editing) return
    await run(editing.name, async (isCurrent) => {
      setSaveError('')
      try {
        await saveUserSkillInstructions(editing.name, editing.text, { expectedSha256: editing.sha256 })
      } catch (error) {
        if (isCurrent()) setSaveError(authorFacingError(error, '技能说明没有保存成功。'))
        throw error
      }
      if (isCurrent()) setEditing(null)
      await changed(isCurrent, S.saved(editing.name))
    }, '技能说明没有保存成功。')
  }

  const create = async () => {
    const name = newName.trim()
    const description = newDesc.trim()
    if (!name || !description || !newContent.trim()) return
    await run('new', async (isCurrent) => {
      const result = await createUserSkill(name, description, newContent)
      if (isCurrent()) {
        setNewName('')
        setNewDesc('')
        setNewContent('')
        setAdding(false)
      }
      await changed(isCurrent, S.created(name), result.skills)
    }, S.createFailed)
  }

  const cancelCreate = () => {
    const discard = () => { setAdding(false); setNewName(''); setNewDesc(''); setNewContent('') }
    if (newDirty) askConfirm({ title: '新技能尚未保存', confirmText: '放弃新建', onConfirm: discard })
    else discard()
  }

  if (editing) {
    return (
      <div className="zm-skill-editor" data-setting="skill-list" tabIndex={-1}><Panel title={S.editTitle(editing.name)}>
        <p className="zm-faint">{editing.editable ? S.editHint : S.readOnlyHint}</p>
        <p className="zm-faint">来源：{editing.source}</p><code className="zm-path">{editing.folder}/SKILL.md</code>
        {saveError ? <p className="zm-setting-feedback is-error" role="alert">{saveError} 草稿仍保留，可复制内容后返回列表重新读取。</p> : null}
        <Textarea
          aria-label="技能说明"
          className="zm-skill-source-editor"
          rows={22}
          value={editing.text}
          readOnly={!editing.editable || busy !== null}
          onChange={(event) => setEditing({ ...editing, text: event.target.value })}
        />
        <div className="zm-btn-row zm-set-save"><span className="zm-save-state">{editing.editable ? editing.text !== editing.original ? '有未保存的修改' : '已保存' : '共享来源 · 只读'}</span>
          {editing.editable ? (
            <Button variant="primary" disabled={busy !== null} busy={busy === editing.name} onClick={() => void save()}>{S.save}</Button>
          ) : null}
          <Button variant="quiet" disabled={busy !== null} onClick={() => {
            if (editing.editable && editing.text !== editing.original) askConfirm({ title: '技能内容尚未保存', confirmText: '放弃修改并返回', onConfirm: () => setEditing(null) })
            else setEditing(null)
          }}>{T.common.back}</Button>
        </div>
      </Panel></div>
    )
  }

  return (
    <>
      <section className="zm-card" aria-label={S.title} aria-busy={busy !== null} data-setting="skill-list" tabIndex={-1}>
        <div className="zm-card-title">
          <h3>{S.countLine(enabledCount, skills.length)}</h3>
          <span className="zm-spacer" />
          <Button small icon={<Plus />} disabled={busy !== null} active={adding} aria-expanded={adding} onClick={() => setAdding((value) => !value)}>{S.add}</Button>
          <Button small icon={<RefreshCw />} variant="quiet" disabled={busy !== null} busy={busy === 'refresh'} onClick={() => void refresh()}>{S.refresh}</Button>
        </div>
        <div className="zm-settings-filter" hidden={adding}>
          <label className="zm-settings-search"><Search aria-hidden /><Input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索技能" aria-label="搜索技能" /></label>
          <Select aria-label="技能状态" value={filter} onChange={(event) => setFilter(event.target.value)}><option value="all">全部状态</option><option value="on">已启用</option><option value="off">未启用</option></Select>
          <Select aria-label="技能来源筛选" value={sourceFilter} onChange={(event) => setSourceFilter(event.target.value)}><option value="all">全部来源</option>{Array.from(new Set(skills.map((skill) => skill.source_label || '织梦'))).map((name) => <option key={name}>{name}</option>)}</Select>
        </div>
        {adding ? (
          <div className="zm-card-inset">
            <Field label={S.newName}>
              <Input value={newName} disabled={busy !== null} placeholder={S.newNamePlaceholder} onChange={(event) => setNewName(event.target.value)} />
            </Field>
            <Field label={S.newDesc}>
              <Input value={newDesc} disabled={busy !== null} placeholder={S.newDescPlaceholder} onChange={(event) => setNewDesc(event.target.value)} />
            </Field>
            <Field label="使用方法" hint="支持 Markdown。写下具体做法，保存为这个技能的正文。">
              <Textarea rows={12} value={newContent} disabled={busy !== null} placeholder="例如：先读取本周工作记录，按已完成、进行中和待解决整理周报，并保留事项对应的来源。" onChange={(event) => setNewContent(event.target.value)} />
            </Field>
            <div className="zm-btn-row">
              <Button variant="primary" disabled={busy !== null || !newName.trim() || !newDesc.trim() || !newContent.trim()} busy={busy === 'new'} onClick={() => void create()}>{S.createIt}</Button>
              <Button variant="quiet" disabled={busy !== null} onClick={cancelCreate}>{T.common.cancel}</Button>
            </div>
          </div>
        ) : null}
        {state === 'loading' ? <Empty>{S.loading}</Empty> : null}
        {state === 'ready' && skills.length === 0 ? <Empty>{S.none}</Empty> : null}
        {state === 'error' ? <Empty>{S.loadFailed}<Button small disabled={busy !== null} onClick={() => void refresh()}>{T.common.retry}</Button></Empty> : null}
        {state === 'ready' && skills.length > 0 && filtered.length === 0 ? <Empty>没有匹配的技能</Empty> : null}
        {skills.length > 0 && !adding ? (
          <ul className="zm-rows">
            {filtered.map((skill) => (
              <li key={skill.name} className={`zm-row zm-skill${skill.enabled ? '' : ' is-off'}`}>
                <div className="zm-row-text">
                  <span className="zm-row-label" title={S.whereLine(skill.folder)}>
                    {skill.name}
                    {skill.source_label ? <span className="zm-chip">{skill.source_label}</span> : null}
                  </span>
                  <span className="zm-row-hint zm-clamp-2">{skill.description || S.noDesc}</span>
                  {skill.shadowed && skill.shadowed.length > 0 ? (
                    <span className="zm-row-hint zm-path">{S.shadowedLine(skill.shadowed)}</span>
                  ) : null}
                </div>
                <div className="zm-row-control">
                  <Button small variant="quiet" disabled={busy !== null} busy={busy === skill.name} onClick={() => void open(skill)}>
                    {skill.origin === 'project' ? S.edit : S.read}
                  </Button>
                  {skill.origin === 'external' ? (
                    <Button small variant="quiet" title={S.external} disabled={busy !== null} busy={busy === skill.name} onClick={() => void copyIn(skill)}>{S.copyIn}</Button>
                  ) : null}
                  <Switch checked={skill.enabled} label={L.skillSwitch(skill.name)} disabled={busy !== null} onChange={() => void toggle(skill)} />
                </div>
              </li>
            ))}
          </ul>
        ) : null}
        {root ? <p className="zm-faint zm-path">{S.rootLine(root)}</p> : null}
      </section>

      <SettingsDisclosure id="skill-sources" title="技能来源" open={pane === 'sources'} onToggle={() => setPane(pane === 'sources' ? 'library' : 'sources')}>
      {installed.length > 0 || missing.length > 0 ? (
        <section className="zm-card" aria-label={S.sourcesTitle} data-setting="skill-sources" tabIndex={-1}>
          <div className="zm-card-title"><h3>{S.sourcesTitle}</h3></div>
          <p className="zm-faint">{S.sourcesIntro}</p>
          <ul className="zm-rows">
            {installed.map((source) => (
              <li key={source.key} className="zm-row">
                <div className="zm-row-text">
                  <span className="zm-row-label">{source.label}</span>
                  <span className="zm-row-hint zm-path">{S.sourceLine(source.count, source.path)}</span>
                </div>
                <div className="zm-row-control">
                  <Switch checked={source.enabled} label={L.sourceSwitch(source.label)} disabled={busy !== null} onChange={() => void toggleSource(source)} />
                </div>
              </li>
            ))}
          </ul>
          {missing.length > 0 ? <details className="zm-set-help"><summary>未发现的来源（{missing.length}）</summary><p className="zm-faint">{S.missingSources(missing)}</p></details> : null}
        </section>
      ) : <Empty title="暂未发现共享技能目录">刷新技能库以重新检查本机来源。</Empty>}
      </SettingsDisclosure>
    </>
  )
}

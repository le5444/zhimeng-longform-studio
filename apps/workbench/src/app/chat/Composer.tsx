import { useEffect, useLayoutEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode, type Ref } from 'react'
import { ArrowUp, BookOpen, ChevronLeft, ChevronRight, FileText, ImagePlus, Lock, Search, ShieldCheck, Square, SquareTerminal, X } from 'lucide-react'
import type { ProjectFile } from '../../api/projects'
import { T } from '../strings'
import { commandOptions, commandQuery, mentionAt, mentionOptions, type CommandAction, type CommandOption, type MentionOption } from './composerLogic'
import { formatFileMention } from '../lib/fileReferences'
import { ComposerPopover } from './ComposerPopover'
import './composer.css'
import { isImageFile } from '../../api/agentImages'
import { DraftImages } from './ImageAttachments'
import type { DraftImage } from '../lib/useConversationImages'
import { composerText } from './composer.text'

const FILE_PAGE_SIZE = 40

function fitInput(node: HTMLTextAreaElement | null) {
  if (!node || node.clientWidth === 0) return
  node.style.height = 'auto'
  node.style.height = `${node.scrollHeight}px`
}

export interface ComposerProps {
  draft: string
  onDraft: (text: string) => void
  onSubmit: () => void
  /** 沿当前一轮补充指令，后台在完整步骤结束后接收。 */
  onSteer?: () => void
  onStop: () => void
  running: boolean
  busy: boolean
  available: boolean
  inputDisabled: boolean
  placeholder: string
  /** 打开了文件夹才有文件可 @；没打开是 null。 */
  files: readonly ProjectFile[] | null
  skills: readonly string[]
  attachedSkills: readonly string[]
  onAttachSkill: (name: string) => void
  onDetachSkill: (name: string) => void
  onOpenSkills?: () => void
  onCommand: (action: CommandAction) => void
  queuePanel: ReactNode
  queueBusy: boolean
  /** 上一句用户说的话：输入框空着时按 ↑ 调出来改。 */
  lastUserText: string | null
  accessFull: boolean
  onToggleAccess: () => void
  folderControl: ReactNode
  modelControls: ReactNode
  goalControls?: ReactNode
  onOpenFolder?: () => void
  textareaRef: Ref<HTMLTextAreaElement>
  images?: DraftImage[]
  imagesBlocked?: boolean
  onImages?: (files: File[]) => void
  onRemoveImage?: (id: string) => void
  onRetryImage?: (id: string) => void
}

/** 对话底下的输入框：打字、@ 文件、/ 命令和技能、换模型、切只读、发送 / 停止 / 排队。 */
export function Composer(props: ComposerProps) {
  const { draft, onDraft, files } = props
  const [cursor, setCursor] = useState(0)
  const [index, setIndex] = useState(0)
  const [panel, setPanel] = useState<'files' | 'skills' | null>(null)
  const [menuDismissed, setMenuDismissed] = useState(false)
  const [draggingImage, setDraggingImage] = useState(false)
  const fileInput = useRef<HTMLInputElement | null>(null)
  const [query, setQuery] = useState('')
  const [filePage, setFilePage] = useState(0)
  const optionsRef = useRef<HTMLDivElement | null>(null)
  const filesButton = useRef<HTMLButtonElement | null>(null)
  const skillsButton = useRef<HTMLButtonElement | null>(null)
  const insertion = useRef({ start: 0, end: 0 })
  const localRef = useRef<HTMLTextAreaElement | null>(null)
  const setRefs = (node: HTMLTextAreaElement | null) => {
    localRef.current = node
    const outer = props.textareaRef
    if (typeof outer === 'function') outer(node)
    else if (outer) (outer as { current: HTMLTextAreaElement | null }).current = node
  }

  useEffect(() => setMenuDismissed(false), [draft, cursor])
  const command = menuDismissed ? null : commandQuery(draft)
  const commands = useMemo(() => (command === null ? [] : commandOptions(command, props.skills)), [command, props.skills])
  const mention = !menuDismissed && command === null ? mentionAt(draft, cursor) : null
  const mentions = useMemo(() => (mention && files ? mentionOptions(files, mention.query) : []), [files, mention])
  const menuOpen = command !== null || mention !== null
  const menuSize = command !== null ? Math.min(commands.length, 10) : Math.min(mentions.length, 8)
  const matchingSkills = props.skills.filter((name) => name.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase()))
  const matchingFiles = useMemo(() => panel === 'files' ? mentionOptions(files ?? [], query, files?.length ?? 0) : [], [panel, files, query])
  const currentFilePage = Math.min(filePage, Math.max(0, Math.ceil(matchingFiles.length / FILE_PAGE_SIZE) - 1))
  const fileStart = currentFilePage * FILE_PAGE_SIZE
  useEffect(() => { if (optionsRef.current) optionsRef.current.scrollTop = 0 }, [currentFilePage, query])
  useEffect(() => setIndex(0), [command, mention?.query])
  const attachSkill = (name: string) => {
    props.onAttachSkill(name)
    setPanel(null)
    localRef.current?.focus()
  }
  const openPanel = (next: 'files' | 'skills') => {
    const node = localRef.current
    insertion.current = { start: node?.selectionStart ?? draft.length, end: node?.selectionEnd ?? draft.length }
    setQuery('')
    setFilePage(0)
    setPanel((value) => value === next ? null : next)
  }
  const addFile = (option: MentionOption) => {
    const { start, end } = insertion.current
    const before = draft.slice(0, start)
    const token = `${before && !/\s$/.test(before) ? ' ' : ''}${formatFileMention(option.path)} `
    const position = start + token.length
    onDraft(before + token + draft.slice(end))
    setPanel(null)
    window.requestAnimationFrame(() => { localRef.current?.focus(); localRef.current?.setSelectionRange(position, position); setCursor(position) })
  }

  // Width changes also reflow text, including opening a panel or returning from settings.
  useLayoutEffect(() => fitInput(localRef.current), [draft])
  useLayoutEffect(() => {
    const node = localRef.current
    if (!node) return
    let width = node.clientWidth
    const resize = () => {
      if (node.clientWidth === width) return
      width = node.clientWidth
      fitInput(node)
    }
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(resize)
    observer?.observe(node)
    window.addEventListener('resize', resize)
    return () => { observer?.disconnect(); window.removeEventListener('resize', resize) }
  }, [])

  const pickCommand = (option: CommandOption) => {
    if (option.kind === 'skill') {
      props.onAttachSkill(option.label)
      onDraft('')
    } else if (option.action) {
      onDraft('')
      props.onCommand(option.action)
    }
    setIndex(0)
    if (option.action !== 'pick-model') localRef.current?.focus()
  }
  const pickMention = (option: MentionOption) => {
    if (!mention) return
    const before = draft.slice(0, mention.start)
    const insert = `${formatFileMention(option.path)} `
    onDraft(`${before}${insert}${draft.slice(mention.end)}`)
    const position = before.length + insert.length
    window.setTimeout(() => {
      localRef.current?.focus()
      localRef.current?.setSelectionRange(position, position)
      setCursor(position)
    }, 0)
  }

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.nativeEvent.isComposing) return
    if (menuOpen && menuSize > 0) {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault()
        setIndex((current) => (current + (event.key === 'ArrowDown' ? 1 : menuSize - 1)) % menuSize)
        return
      }
      if ((event.key === 'Enter' || event.key === 'Tab') && !event.shiftKey) {
        event.preventDefault()
        if (command !== null) pickCommand(commands[Math.min(index, commands.length - 1)])
        else pickMention(mentions[Math.min(index, mentions.length - 1)])
        return
      }
    }
    if (menuOpen && event.key === 'Escape') {
      event.preventDefault()
      setMenuDismissed(true)
      return
    }
    if (event.key === 'ArrowUp' && !event.shiftKey && !draft.trim() && props.lastUserText) {
      event.preventDefault()
      onDraft(props.lastUserText)
      return
    }
    if (event.key === 'Enter' && !event.shiftKey) {
      event.preventDefault()
      if (canSend) props.onSubmit()
    }
  }

  const hasContent = Boolean(draft.trim()) || Boolean(props.images?.length)
  const canSend = props.available && !props.queueBusy && !props.inputDisabled && !props.imagesBlocked && hasContent
  return (
    <div className={`zm-composer-wrap zm-compose-surface${draggingImage ? ' is-image-drop' : ''}`}
      onDragOver={(event) => { if (event.dataTransfer.types.includes('Files') && !props.inputDisabled) { event.preventDefault(); event.dataTransfer.dropEffect = 'copy'; setDraggingImage(true) } }}
      onDragLeave={(event) => { if (!event.currentTarget.contains(event.relatedTarget as Node)) setDraggingImage(false) }}
      onDrop={(event) => { if (!event.dataTransfer.files.length) return; event.preventDefault(); setDraggingImage(false); if (!props.inputDisabled) props.onImages?.([...event.dataTransfer.files]) }}>
      {props.queuePanel}
      {panel ? <ComposerPopover key={panel} anchor={panel === 'files' ? filesButton : skillsButton} title={panel === 'files' ? '引用文件' : '选择技能'} onClose={(restore) => { setPanel(null); if (restore) localRef.current?.focus() }}>
        <label className="zm-compose-search"><Search aria-hidden /><input value={query} onChange={(event) => { setQuery(event.target.value); setFilePage(0) }} aria-label={panel === 'files' ? '搜索项目文件' : '搜索可用技能'} placeholder={panel === 'files' ? '搜索项目文件' : '搜索可用技能'} /></label>
        <div className="zm-compose-options" ref={optionsRef}>
          {panel === 'files' ? matchingFiles.slice(fileStart, fileStart + FILE_PAGE_SIZE).map((option) => <button type="button" key={option.fileId} onClick={() => addFile(option)}><FileText aria-hidden /><span>{option.name}<small>{option.folder}</small></span></button>)
            : matchingSkills.map((name) => <button type="button" key={name} onClick={() => attachSkill(name)}><BookOpen aria-hidden /><span>{name}</span>{props.attachedSkills.includes(name) ? <small>已添加</small> : null}</button>)}
          {panel === 'files' && matchingFiles.length === 0 ? <p>{!files ? '打开文件夹后，可以引用其中的文件。' : '没有匹配的文件。'}</p> : null}
          {panel === 'skills' && matchingSkills.length === 0 ? <p>{props.skills.length ? '没有匹配的技能。' : '还没有可用技能，可以在技能库里启用。'}</p> : null}
        </div>
        {panel === 'files' && matchingFiles.length > FILE_PAGE_SIZE ? <div className="zm-compose-pagination">
          <button type="button" disabled={currentFilePage === 0} aria-label={composerText.previousFiles} title={composerText.previousFiles} onClick={() => setFilePage(currentFilePage - 1)}><ChevronLeft aria-hidden /></button>
          <span role="status">{composerText.filePage(fileStart + 1, Math.min(fileStart + FILE_PAGE_SIZE, matchingFiles.length), matchingFiles.length)}</span>
          <button type="button" disabled={fileStart + FILE_PAGE_SIZE >= matchingFiles.length} aria-label={composerText.nextFiles} title={composerText.nextFiles} onClick={() => setFilePage(currentFilePage + 1)}><ChevronRight aria-hidden /></button>
        </div> : null}
        {panel === 'files' && !files && props.onOpenFolder ? <button type="button" className="zm-compose-library" onClick={() => { setPanel(null); props.onOpenFolder?.() }}>打开文件夹</button> : null}
        {panel === 'skills' && props.onOpenSkills ? <button type="button" className="zm-compose-library" onClick={() => { setPanel(null); props.onOpenSkills?.() }}>打开技能库</button> : null}
      </ComposerPopover> : null}
      {!panel && command !== null ? (
        <div className="zm-popover" role="listbox" aria-label={T.agent.slash.title}>
          {commands.length === 0 ? <p className="zm-popover-note">{T.agent.slash.none}</p> : commands.slice(0, 10).map((option, position) => (
            <button
              key={option.key}
              type="button"
              role="option"
              aria-selected={position === index}
              className={`zm-popover-item${position === index ? ' is-active' : ''}`}
              onMouseEnter={() => setIndex(position)}
              onClick={() => pickCommand(option)}
            >
              {option.kind === 'skill' ? <BookOpen aria-hidden /> : <SquareTerminal aria-hidden />}
              <span className="zm-popover-label">{option.label}</span>
              <span className="zm-popover-hint">{option.kind === 'skill' ? T.agent.slash.groupSkill : option.hint}</span>
            </button>
          ))}
        </div>
      ) : null}
      {!panel && mention ? (
        <div className="zm-popover" role="listbox" aria-label={T.agent.mention.title}>
          {!files ? <p className="zm-popover-note">{T.agent.mention.unbound}</p> : mentions.length === 0 ? <p className="zm-popover-note">{T.agent.mention.none}</p> : mentions.map((option, position) => (
            <button
              key={option.fileId}
              type="button"
              role="option"
              aria-selected={position === index}
              className={`zm-popover-item${position === index ? ' is-active' : ''}`}
              onMouseEnter={() => setIndex(position)}
              onClick={() => pickMention(option)}
            >
              <FileText aria-hidden />
              <span className="zm-popover-label">{option.name}</span>
              <span className="zm-popover-hint">{option.folder}</span>
            </button>
          ))}
        </div>
      ) : null}
      <input ref={fileInput} type="file" hidden multiple accept="image/*" aria-label="选择图片文件" onChange={(event) => {
        const images = [...(event.target.files ?? [])]; if (images.length) props.onImages?.(images)
        event.target.value = ''; setPanel(null); localRef.current?.focus()
      }} />
      <div className="zm-compose-box">
      <form className="zm-composer" onSubmit={(event) => { event.preventDefault(); if (canSend) props.onSubmit() }}>
        {draggingImage ? <p className="zm-image-drop-hint">松开以添加图片</p> : null}
        <DraftImages images={props.images ?? []} onRemove={props.onRemoveImage ?? (() => {})} onRetry={props.onRetryImage ?? (() => {})} />
        {props.attachedSkills.length > 0 ? (
          <div className="zm-composer-skills">
            {props.attachedSkills.map((name) => (
              <span key={name} className="zm-chip is-skill">
                <BookOpen aria-hidden />
                <span>{name}</span>
                <button type="button" aria-label={T.agent.skillTagRemove(name)} title={T.agent.skillTagRemove(name)} onClick={() => props.onDetachSkill(name)}>
                  <X aria-hidden />
                </button>
              </span>
            ))}
          </div>
        ) : null}
        <textarea
          ref={setRefs}
          className="zm-composer-input"
          value={draft}
          rows={1}
          placeholder={props.placeholder}
          aria-label={props.placeholder}
          disabled={props.inputDisabled}
          onFocus={() => setPanel(null)}
          onChange={(event) => { onDraft(event.target.value); setCursor(event.target.selectionStart) }}
          onClick={(event) => setCursor(event.currentTarget.selectionStart)}
          onKeyUp={(event) => setCursor(event.currentTarget.selectionStart)}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            const items = [...(event.clipboardData.items ?? [])].filter((item) => item.kind === 'file').map((item) => item.getAsFile()).filter((file): file is File => file !== null && isImageFile(file))
            const images = items.length ? items : [...event.clipboardData.files].filter(isImageFile)
            if (images.length && !props.inputDisabled) { event.preventDefault(); props.onImages?.(images) }
          }}
        />
        <div className="zm-compose-toolbar">
          <div className="zm-compose-actions">
            <button ref={filesButton} type="button" className="zm-compose-action" aria-label="引用文件" title={files ? '在输入位置引用项目文件' : '打开文件夹后可选择文件'} aria-expanded={panel === 'files'} aria-haspopup="dialog" disabled={props.inputDisabled} onClick={() => openPanel('files')}><FileText aria-hidden /><span>文件</span></button>
            <button ref={skillsButton} type="button" className="zm-compose-action" data-skill-picker="" aria-label="选择技能" aria-expanded={panel === 'skills'} aria-haspopup="dialog" disabled={props.inputDisabled} onClick={() => openPanel('skills')}><BookOpen aria-hidden /><span>技能</span></button>
            <button type="button" className="zm-compose-action" aria-label="添加图片" title="选择图片，也可直接粘贴或拖入" disabled={props.inputDisabled} onClick={() => { setPanel(null); fileInput.current?.click() }}><ImagePlus aria-hidden /><span>图片</span></button>
          </div>
          <button type="button" className={`zm-compose-access${props.accessFull ? '' : ' is-warn'}`} aria-pressed={props.accessFull} disabled={props.running || props.busy} title={props.accessFull ? T.agent.access.fullHint : T.agent.access.readOnlyHint} onClick={props.onToggleAccess}>
            {props.accessFull ? <ShieldCheck aria-hidden /> : <Lock aria-hidden />}<span>{props.accessFull ? T.agent.access.full : T.agent.access.readOnly}</span></button>
          <div className={`zm-compose-send${props.running && hasContent ? ' has-draft' : ''}`}>
          {props.running ? (
            <>
              {hasContent ? (
                <>
                <button type="submit" className="zm-send is-queue" disabled={!canSend} title={T.agent.queueSend}>
                  <span>{T.agent.queueSend}</span>
                </button>
                {props.onSteer ? <button type="button" className="zm-send is-steer" disabled={!canSend}
                  title="等当前步骤结束后，送入正在进行的这一轮" onClick={props.onSteer}>引导这一轮</button> : null}
                </>
              ) : null}
              <button type="button" className="zm-send is-stop" onClick={props.onStop} aria-label={T.agent.stop} title={T.agent.stop}>
                <Square aria-hidden />
              </button>
            </>
          ) : props.busy ? (
            <button type="submit" className="zm-send is-queue" disabled={!canSend} title={T.agent.queueSend}>{T.agent.queueSend}</button>
          ) : (
            <button type="submit" className="zm-send" disabled={!canSend} aria-label={T.agent.send} title={T.agent.sendHint}>
              <ArrowUp aria-hidden />
            </button>
          )}
          </div>
        </div>
      </form>
      <div className="zm-compose-model">{props.modelControls}{props.goalControls}</div>
      </div>
    </div>
  )
}

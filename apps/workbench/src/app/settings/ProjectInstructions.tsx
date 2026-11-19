import { useRef, useState } from 'react'
import { FileText, Plus } from 'lucide-react'
import { inspectBoundProject, type ProjectInspection } from '../../api/projects'
import { applyChangeSetDirect, createProjectChangeSet, getChangeSet, type ChangeSet } from '../../api/changeSets'
import { authorFacingError } from '../../lib/authorFacingError'
import { Btn } from '../ui'
import { Textarea } from './kit'
import { S } from './settingsText'
import { useSettingsDraft } from './drafts'

export function ProjectInstructions({ snapshot, onOpen, onSaved }: {
  snapshot: ProjectInspection | null; onOpen: (path: string) => void; onSaved?: () => void
}) {
  const [creating, setCreating] = useState(false)
  const [text, setText] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const [saved, setSaved] = useState<ProjectInspection | null>(null)
  const pending = useRef<ChangeSet | null>(null)
  const operation = useRef(false)
  useSettingsDraft('project', Boolean(text.trim()), '项目说明 AGENTS.md')
  if (!snapshot) return <div className="zm-settings-empty" data-setting="project-instructions"><FileText /><h2>当前未打开项目</h2><p>回到工作台打开一个文件夹，即可查看和编辑它的项目说明。</p></div>
  const current = saved && saved.project.revision >= snapshot.project.revision ? saved : snapshot
  const names = ['AGENTS.md', 'CLAUDE.md', '写作笔记.md']
  const files = names.flatMap(name => current.files.filter(file => file.relative_path.toLowerCase() === name.toLowerCase()))
  const create = async () => {
    if (operation.current || !text.trim()) return
    operation.current = true
    setBusy(true); setError('')
    try {
      let record = pending.current
      if (record) {
        record = await getChangeSet(record.change_set_id)
        if (record.status === 'conflicted' || record.status === 'discarded') {
          pending.current = null
          throw new Error('原修改记录未应用。草稿已恢复可编辑，请核对项目后重新保存。')
        }
      }
      else {
        const fresh = await inspectBoundProject(current.project.project_id)
        record = await createProjectChangeSet(fresh.project.project_id, {
          base_revision: fresh.project.revision, base_fingerprint: fresh.project.fingerprint,
          title: '创建项目说明 AGENTS.md', summary: '在设置中创建项目约定', origin: 'manual',
          changes: [{ operation: 'create', relative_path: 'AGENTS.md', after_content: text, rationale: '用户填写的项目说明' }],
        })
        pending.current = record
      }
      if (record.status !== 'applied') {
        try { record = await applyChangeSetDirect(record.change_set_id, record.review_revision) }
        catch (error) {
          const recovered = await getChangeSet(record.change_set_id)
          if (recovered.status !== 'applied') {
            if (recovered.status === 'conflicted' || recovered.status === 'discarded') pending.current = null
            throw error
          }
          record = recovered
        }
      }
      if (record.status !== 'applied') throw new Error('保存结果尚未确认，请重试核对原修改记录。')
      const fresh = await inspectBoundProject(current.project.project_id)
      setSaved(fresh); setText(''); setCreating(false); pending.current = null
      onSaved?.()
    } catch (error) { setError(authorFacingError(error, '项目说明未保存，输入仍保留。')) }
    finally { operation.current = false; setBusy(false) }
  }
  return <section className="zm-card" data-setting="project-instructions" tabIndex={-1}>
    <div className="zm-card-title"><h3>{S.projectInstructions(current.project.display_name)}</h3></div>
    <p className="zm-faint">{S.projectInstructionsIntro}</p><code className="zm-project-location">{current.project.root_path}</code>
    <p className="zm-faint">按 AGENTS.md、CLAUDE.md、写作笔记.md 的顺序读取；每份最多读取开头 32 KiB。仅作用于关联此文件夹的对话。</p>
    <div className="zm-project-instructions">{files.map(file => <div className="zm-project-file" key={file.file_id}>
      <FileText /><div><strong>{file.relative_path}</strong><span>项目根目录 · {file.size_bytes > 32768 ? '超出 32 KiB，仅开头进入上下文' : '作为项目上下文读取'}</span></div><Btn size="sm" onClick={() => onOpen(file.relative_path)}>打开 {file.relative_path}</Btn>
    </div>)}</div>
    {!files.length ? <p className="zm-faint">{S.projectInstructionsNone}</p> : null}
    {!files.some(file => file.relative_path.toLowerCase() === 'agents.md') && !creating ? <Btn icon={<Plus />} onClick={() => setCreating(true)}>创建 AGENTS.md</Btn> : null}
    {creating ? <div className="zm-settings-section">
      <label className="zm-field">项目约定<Textarea aria-label="项目约定" rows={12} value={text} disabled={busy || Boolean(pending.current)} onChange={event => setText(event.target.value)} placeholder="写下这个文件夹的用途、工作方式和需要长期遵守的约定。" /></label>
      <p className="zm-faint">保存为项目根目录的 AGENTS.md；可在工作台的修改记录中撤回。</p>
      {error ? <p role="alert" className="zm-setting-feedback is-error">{error}</p> : null}
      <div className="zm-btn-row"><Btn variant="primary" disabled={busy || !text.trim()} busy={busy} onClick={() => void create()}>{pending.current ? '核对并继续保存' : '保存项目说明'}</Btn><Btn disabled={busy} onClick={() => setCreating(false)}>收起</Btn></div>
    </div> : null}
  </section>
}

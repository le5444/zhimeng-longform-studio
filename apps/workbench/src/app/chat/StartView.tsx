import { ArrowUpRight, BookOpen, Brain, Cpu, FolderOpen, FolderPlus, PanelsTopLeft } from 'lucide-react'
import type { ProjectConversationGroup } from '../lib/useConversations'
import { A } from '../text'
import { Btn } from '../ui'

export function StartView({ projectName, projects, model, onOpenFolder, onNewFolder, onProject, onModels, onMemory, onSkills }: {
  projectName: string | null
  projects: readonly ProjectConversationGroup[]
  model: string | null
  onOpenFolder: () => void
  onNewFolder: () => void
  onProject: (id: string) => void
  onModels: () => void
  onMemory: () => void
  onSkills?: () => void
}) {
  const H = A.home
  return <div className="zm-start">
    <header><PanelsTopLeft aria-hidden /><div><h2>{projectName ? A.chat.emptyTitleFolder : A.chat.emptyTitle}</h2><p>{projectName ?? '直接提问，或打开文件夹一起处理文件。'}</p></div></header>
    <div className="zm-start-actions">
      <Btn icon={<FolderOpen />} onClick={onOpenFolder}>{H.open}</Btn>
      <Btn icon={<FolderPlus />} onClick={onNewFolder}>{H.create}</Btn>
    </div>
    {!projectName && projects.length ? <section className="zm-start-projects" aria-label={H.projects}><h3>{H.projects}</h3>
      {projects.slice(0, 3).map((project) => <button key={project.projectId} type="button" onClick={() => onProject(project.projectId)}>
        <FolderOpen aria-hidden /><span><strong>{project.name}</strong><small>{project.rootPath}</small></span><ArrowUpRight aria-hidden />
      </button>)}
    </section> : null}
    <div className="zm-start-links">
      <button type="button" onClick={onModels}><Cpu aria-hidden /><span>{model || H.models}</span></button>
      <button type="button" onClick={onMemory}><Brain aria-hidden /><span>{H.memory}</span></button>
      {onSkills ? <button type="button" onClick={onSkills}><BookOpen aria-hidden /><span>{H.skills}</span></button> : null}
    </div>
  </div>
}

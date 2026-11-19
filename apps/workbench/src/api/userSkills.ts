import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

export interface UserSkillView {
  name: string
  description: string
  folder: string
  has_scripts: boolean
  enabled: boolean
  /** project = 本软件自己的 skills/，设置页能改；external = 共享文件夹（~/.codex/skills 等），只读。 */
  origin: 'project' | 'external'
  /** 找到它的那个技能文件夹。 */
  root?: string
  /** 别的文件夹里没用上的同名副本（同名的以排在前面的文件夹为准）。 */
  shadowed?: string[]
  /** 来自哪一家（技能库里各家的 key；织梦自己的是 zhimeng）和名字。 */
  source?: string
  source_label?: string
}

export interface UserSkillRootView {
  path: string
  origin: 'project' | 'external'
  exists: boolean
  source?: string
  label?: string
}

/** 技能库里的一家：本机某个 agent 的技能文件夹，在不在、几个技能、织梦用不用。 */
export interface SkillSourceView {
  key: string
  label: string
  path: string
  exists: boolean
  count: number
  enabled: boolean
  default_enabled: boolean
}

export interface UserSkillsView {
  schema: 'zhimeng.user-skills.v1'
  skills_root: string
  /** 读技能的文件夹，按「同名听谁的」排好。 */
  roots?: UserSkillRootView[]
  /** 本机各家 agent 的技能文件夹（9-28 起一家一个开关）。 */
  sources?: SkillSourceView[]
  exists: boolean
  total: number
  enabled: number
  skills: UserSkillView[]
}

/** 技能库：用上 / 不用本机某一家 agent 的技能（原地读它的文件夹，不复制、不往里写）。 */
export function setSkillSourceEnabled(
  key: string,
  enabled: boolean,
  options: RuntimeClientOptions = {},
): Promise<UserSkillsView> {
  return fetchRuntimeJson(
    `/api/v1/user-skills/sources/${encodeURIComponent(key)}/${enabled ? 'enable' : 'disable'}`,
    options,
    { method: 'POST' },
  )
}

/** 把别家文件夹里的技能复制一份进织梦自己的技能库，以后在织梦里能改；原来那份不动。 */
export function copyUserSkillToLibrary(
  name: string,
  options: RuntimeClientOptions = {},
): Promise<{ ok: true; message: string; skills: UserSkillsView }> {
  return fetchRuntimeJson(
    `/api/v1/user-skills/${encodeURIComponent(name)}/copy-to-library`,
    options,
    { method: 'POST' },
  )
}

/** 作者技能文件夹：往 skills/ 丢一个带 SKILL.md 的文件夹就自动上架。 */
export function readUserSkills(
  options: RuntimeClientOptions = {},
): Promise<UserSkillsView> {
  return fetchRuntimeJson('/api/v1/user-skills', options)
}

export function setUserSkillEnabled(
  name: string,
  enabled: boolean,
  options: RuntimeClientOptions = {},
): Promise<{ name: string; enabled: boolean }> {
  return fetchRuntimeJson(
    `/api/v1/user-skills/${encodeURIComponent(name)}/${enabled ? 'enable' : 'disable'}`,
    options,
    { method: 'POST' },
  )
}

export function createUserSkill(
  name: string,
  description: string,
  content: string,
  options: RuntimeClientOptions = {},
): Promise<{ ok: true; message: string; skills: UserSkillsView }> {
  return fetchRuntimeJson('/api/v1/user-skills', options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ name, description, content }),
  })
}

export function readUserSkillInstructions(
  name: string,
  options: RuntimeClientOptions = {},
): Promise<{ name: string; content: string; editable: boolean; sha256?: string }> {
  return fetchRuntimeJson(
    `/api/v1/user-skills/${encodeURIComponent(name)}/instructions`,
    options,
  )
}

/** 作者确认后把优化过的 SKILL.md 全文写回（只允许项目内技能）。 */
export function saveUserSkillInstructions(
  name: string,
  content: string,
  options: RuntimeClientOptions & { expectedSha256?: string } = {},
): Promise<{ name: string; ok: boolean; message: string }> {
  return fetchRuntimeJson(
    `/api/v1/user-skills/${encodeURIComponent(name)}/instructions`,
    options,
    {
      method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ content, expected_sha256: options.expectedSha256 }),
    },
  )
}

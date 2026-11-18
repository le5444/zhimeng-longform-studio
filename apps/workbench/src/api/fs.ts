import { fetchRuntimeJson, type RuntimeClientOptions } from './runtime'

/**
 * 选文件夹用的三个接口（纠偏指令 P2）：浏览器自带的选框拿不到完整路径，所以框自己画，文件夹由后端列。
 * 只列文件夹、不读内容；打开成项目还是「加进受信根 + 读项目」两个老接口。
 */

export interface FsPlace {
  label: string
  path: string
  kind: 'home' | 'desktop' | 'documents' | 'downloads' | 'trusted' | 'recent' | 'drive'
}

export interface FsEntry {
  name: string
  path: string
  is_link: boolean
  /** 已经打开过（是一个项目）。 */
  is_project: boolean
  /** 小说领域的小标记：有《这本书.md》或 01_项目定位。 */
  looks_like_book: boolean
}

export interface FsListing {
  schema: 'zhimeng.fs-list.v1'
  path: string
  parent: string | null
  breadcrumbs: Array<{ name: string; path: string }>
  is_project: boolean
  entries: FsEntry[]
  total: number
  truncated: boolean
}

export function listPlaces(options: RuntimeClientOptions = {}): Promise<{ places: FsPlace[] }> {
  return fetchRuntimeJson('/api/v1/fs/places', options)
}

export function listFolder(path: string, options: RuntimeClientOptions = {}): Promise<FsListing> {
  return fetchRuntimeJson(`/api/v1/fs/list?path=${encodeURIComponent(path)}`, options)
}

export function makeFolder(parent: string, name: string, options: RuntimeClientOptions = {}): Promise<{ path: string }> {
  return fetchRuntimeJson('/api/v1/fs/mkdir', options, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ parent, name }),
  })
}

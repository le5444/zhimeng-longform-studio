/**
 * 本机记住的界面习惯（左栏折没折、右栏开没开、哪些项目置顶或移除）：放 localStorage。
 * 读写失败（隐私模式、存满了）只影响「记住」，不影响这一次的操作，所以一律吞掉。
 */

export function readFlag(key: string, fallback: boolean): boolean {
  try {
    const value = window.localStorage.getItem(key)
    return value === null ? fallback : value === '1'
  } catch {
    return fallback
  }
}

export function writeFlag(key: string, value: boolean): void {
  try {
    window.localStorage.setItem(key, value ? '1' : '0')
  } catch {
    // 本机存不了就只对这一次生效
  }
}

export function readIdSet(key: string): ReadonlySet<string> {
  try {
    const stored = JSON.parse(window.localStorage.getItem(key) ?? '[]') as unknown
    return new Set(Array.isArray(stored) ? stored.filter((item): item is string => typeof item === 'string') : [])
  } catch {
    return new Set()
  }
}

export function writeIdSet(key: string, ids: ReadonlySet<string>): void {
  try {
    window.localStorage.setItem(key, JSON.stringify([...ids]))
  } catch {
    // 本机存不了就只对这一次生效
  }
}

/** 在一个集合里加上或去掉一项，返回新集合（原集合不动）。 */
export function toggled(ids: ReadonlySet<string>, id: string, on?: boolean): ReadonlySet<string> {
  const next = new Set(ids)
  if (on ?? !next.has(id)) next.add(id)
  else next.delete(id)
  return next
}

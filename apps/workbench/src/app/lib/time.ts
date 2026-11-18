/** 左栏对话右边那个日子：今天 / 昨天 / N 天前 / 某月某日 / 某年某月某日。 */
export function shortDay(iso: string | null | undefined, now = new Date()): string {
  if (!iso) return ''
  const date = new Date(iso)
  if (Number.isNaN(date.getTime())) return ''
  const sameDay = (a: Date, b: Date) => a.getFullYear() === b.getFullYear() && a.getMonth() === b.getMonth() && a.getDate() === b.getDate()
  if (sameDay(date, now)) return '今天'
  const yesterday = new Date(now)
  yesterday.setDate(now.getDate() - 1)
  if (sameDay(date, yesterday)) return '昨天'
  const days = Math.floor((now.getTime() - date.getTime()) / 86400000)
  if (days > 1 && days < 7) return `${days} 天前`
  if (date.getFullYear() === now.getFullYear()) return `${date.getMonth() + 1}月${date.getDate()}日`
  return `${date.getFullYear()}年${date.getMonth() + 1}月${date.getDate()}日`
}

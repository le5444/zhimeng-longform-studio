export const composerText = {
  previousFiles: '上一页文件',
  nextFiles: '下一页文件',
  filePage: (start: number, end: number, total: number) => `${start}-${end} / ${total}`,
} as const

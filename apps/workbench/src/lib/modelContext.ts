

export function contextWindowLabel(tier: string | null | undefined): string {
  return tier === '1m' ? '1M' : '256K'
}

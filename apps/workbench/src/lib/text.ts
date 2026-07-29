export function countPlainTextCharacters(content: string): number {
  return content.replace(/^#{1,3}\s+/gm, '').replace(/\s/g, '').length
}

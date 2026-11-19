/** Message wrappers follow document order in the single-column conversation. */
export function firstMessageAfter(nodes: readonly HTMLElement[], edge: 'top' | 'bottom', position: number): number {
  let low = 0
  let high = nodes.length
  while (low < high) {
    const middle = low + Math.floor((high - low) / 2)
    if (nodes[middle].getBoundingClientRect()[edge] <= position) low = middle + 1
    else high = middle
  }
  return low
}

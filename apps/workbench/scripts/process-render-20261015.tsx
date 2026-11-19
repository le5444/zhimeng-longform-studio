import { useState } from 'react'
import { createRoot } from 'react-dom/client'
import { flushSync } from 'react-dom'
import { ProcessGroup } from '../src/app/chat/Message'
import { Markdown } from '../src/app/chat/Markdown'
import '../src/app/zm.css'
import '../src/app/chat/chat.css'

const host = document.getElementById('root')!
const root = createRoot(host)
const paragraph = Array.from({ length: 8 }, (_, i) => `1. Verify item ${i + 1}.\n\n   Keep supporting detail and [source](https://example.com).`).join('\n\n')
const code = '\n\n```python\nprint("complete")\n```'
const source = `# Delivery analysis\n\n${paragraph}${code}`
export function Detail({ index }: { index: number }) {
  const [expanded, setExpanded] = useState(false)
  return <><button onClick={() => setExpanded(!expanded)}>Details {index}</button>{expanded ? <p>Preserved detail {index}</p> : null}
    <Markdown text={source} /></>
}
export function History({ revision }: { revision: number }) {
  return <main style={{ maxWidth: 850, margin: '24px auto', padding: 20 }}>
    <h1>Delivery report</h1><p>Report revision {revision}</p>
    {Array.from({ length: 40 }, (_, index) => <section key={index}>
      <h2>Task {index + 1}</h2><ProcessGroup><Detail index={index} /></ProcessGroup>
      <p>Completed task {index + 1}.</p>
    </section>)}
  </main>
}
Object.assign(window, {
  measureProcessHistory() {
    const samples = []
    for (let round = 0; round < 5; round++) {
      flushSync(() => root.render(null))
      const started = performance.now()
      flushSync(() => root.render(<History revision={round} />))
      const mountedMs = performance.now() - started
      const updateStarted = performance.now()
      flushSync(() => root.render(<History revision={round + 1} />))
      samples.push({ mountedMs, updatedMs: performance.now() - updateStarted, nodes: host.querySelectorAll('*').length,
        markdownDocuments: host.querySelectorAll('.zm-md').length })
    }
    return { groups: 40, charactersPerGroup: source.length, samples }
  },
})

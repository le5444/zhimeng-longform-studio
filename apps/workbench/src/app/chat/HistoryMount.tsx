import { memo, PureComponent, startTransition, useDeferredValue, type Key, type ReactElement, type RefObject } from 'react'
import { firstMessageAfter } from './messageGeometry'
import { measureMessageLayout } from './useMessageLayout'

const BATCH_SIZE = 12

interface Props {
  children: readonly ReactElement[]
  scrollRef: RefObject<HTMLDivElement | null>
  onReady: () => void
}
interface State { first: Key | null; complete: boolean }
interface Position { node: HTMLElement | undefined; offset: number; fromBottom: number; bottom: boolean }

/** Mount whole message groups from the tail, yielding between commits. Stable keys
 * keep every revealed node mounted, even when new messages arrive during loading.
 * The commit snapshot captures scrolling that happened while a transition waited.
 */
export const HistoryMount = memo(function HistoryMount(props: Props) {
  const ready = useDeferredValue(true, false)
  return ready ? <HistoryBatch {...props} /> : <p className="zm-muted-line" role="status">正在显示历史记录…</p>
})

class HistoryBatch extends PureComponent<Props, State, Position | null> {
  state: State = { first: this.props.children[Math.max(0, this.props.children.length - BATCH_SIZE)]?.key ?? null, complete: false }
  private timer: ReturnType<typeof setTimeout> | undefined
  private rounding: { top: number; error: number } | null = null

  private start(props = this.props, state = this.state) {
    return state.complete ? 0 : Math.max(0, props.children.findIndex((child) => child.key === state.first))
  }

  private advance() {
    if (this.state.complete || this.timer !== undefined) return
    if (this.start() === 0) {
      this.setState({ complete: true }, this.props.onReady)
      return
    }
    this.timer = setTimeout(() => {
      this.timer = undefined
      const first = this.props.children[Math.max(0, this.start() - BATCH_SIZE)]?.key ?? null
      startTransition(() => this.setState({ first }))
    }, 24)
  }

  componentDidMount() { this.advance() }
  componentWillUnmount() { clearTimeout(this.timer); this.timer = undefined }

  getSnapshotBeforeUpdate(previous: Props, state: State): Position | null {
    if (state.complete || previous.children[this.start(previous, state)]?.key === this.props.children[this.start()]?.key) return null
    const scroller = this.props.scrollRef.current
    if (!scroller) return null
    const top = scroller.getBoundingClientRect().top
    const nodes = [...scroller.querySelectorAll<HTMLElement>('[data-message-id]')]
    const node = nodes[firstMessageAfter(nodes, 'bottom', top)]
    const fromBottom = scroller.scrollHeight - scroller.scrollTop
    const error = this.rounding?.top === scroller.scrollTop ? this.rounding.error : 0
    return { node, offset: node ? node.getBoundingClientRect().top - top - error : 0, fromBottom, bottom: fromBottom - scroller.clientHeight < 80 }
  }

  componentDidUpdate(_previous: Props, _state: State, position: Position | null) {
    const scroller = this.props.scrollRef.current
    if (scroller && position) {
      // Size only newly mounted messages before enabling offscreen containment.
      measureMessageLayout(scroller)
      this.rounding = null
      if (position.bottom) scroller.scrollTop = scroller.scrollHeight
      else if (position.node?.isConnected) {
        const target = scroller.scrollTop + position.node.getBoundingClientRect().top - scroller.getBoundingClientRect().top - position.offset
        scroller.scrollTop = target
        // Some browsers round scrollTop to pixels. Carry the remainder so dozens
        // of prepends cannot accumulate drift; a later user scroll discards it.
        const error = target - scroller.scrollTop
        if (Math.abs(error) < 1) this.rounding = { top: scroller.scrollTop, error }
      }
      else scroller.scrollTop = scroller.scrollHeight - position.fromBottom
    }
    this.advance()
  }

  render() {
    const start = this.start()
    return <>
      {start > 0 ? <p key="history-loading" className="zm-muted-line" role="status">正在显示历史记录…</p> : null}
      {this.props.children.slice(start)}
    </>
  }
}

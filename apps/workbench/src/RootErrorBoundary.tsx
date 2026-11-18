import { Component, type ReactNode } from 'react'

interface ErrorBoundaryState {
  error: Error | null
}

/**
 * 渲染崩溃兜底：任何一个组件抛异常时显示可恢复的错误页，
 * 而不是白屏。作者正在写的正文由编辑器自身的本地草稿缓存保护，
 * 这里只兜"界面崩了"的底。
 */
export class RootErrorBoundary extends Component<{ children: ReactNode }, ErrorBoundaryState> {
  state: ErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { error }
  }

  render() {
    if (this.state.error) {
      return (
        <div
          style={{
            width: '100vw',
            height: '100vh',
            display: 'flex',
            flexDirection: 'column',
            alignItems: 'center',
            justifyContent: 'center',
            gap: 12,
            background: '#16161e',
            color: '#e0dde4',
            fontFamily: 'system-ui, sans-serif',
            padding: 24,
            textAlign: 'center',
          }}
        >
          <strong style={{ fontSize: 18 }}>界面出了一个问题</strong>
          <p style={{ maxWidth: 480, color: '#9a97a1', lineHeight: 1.6, margin: 0 }}>
            你的正文和设定不受影响（都存在电脑里）。
            点下面的按钮重新打开工作台。
          </p>
          <button
            type="button"
            onClick={() => { window.location.href = '/' }}
            style={{
              padding: '8px 24px',
              borderRadius: 8,
              border: '1px solid #e0653a',
              background: 'transparent',
              color: '#e0653a',
              cursor: 'pointer',
              fontSize: 14,
            }}
          >
            重新打开
          </button>
          <details style={{ maxWidth: 560, width: '100%', marginTop: 8 }}>
            <summary style={{ cursor: 'pointer', color: '#6a6771', fontSize: 12 }}>技术详情</summary>
            <pre style={{
              textAlign: 'left',
              fontSize: 11,
              color: '#6a6771',
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              maxHeight: 200,
              overflow: 'auto',
              padding: 8,
              borderRadius: 6,
              background: 'rgba(255,255,255,0.04)',
            }}>{this.state.error.stack ?? this.state.error.message}</pre>
          </details>
        </div>
      )
    }
    return this.props.children
  }
}

// 织梦的界面（2026-09-29 回炉重造，照普通 agent 的样子：左栏对话和项目、中间对话、右栏文件和修改）。全在 src/app，根是 Root.tsx。
import { Suspense, lazy } from 'react'
import { RootErrorBoundary } from './RootErrorBoundary'
import { StrictMode } from 'react'
import { createRoot } from 'react-dom/client'
import './index.css'

const Root = lazy(() => import('./app/Root'))

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <RootErrorBoundary>
      <Suspense fallback={<div style={{ width: '100vw', height: '100vh' }} />}>
        <Root />
      </Suspense>
    </RootErrorBoundary>
  </StrictMode>,
)

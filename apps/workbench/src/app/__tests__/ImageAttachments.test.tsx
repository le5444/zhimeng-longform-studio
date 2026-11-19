import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import { UserMessage } from '../chat/Message'
import { MessageImages } from '../chat/ImageAttachments'

const api = vi.hoisted(() => ({ read: vi.fn() }))
vi.mock('../../api/agentImages', () => ({ readAgentImage: api.read }))
beforeEach(() => {
  api.read.mockReset().mockResolvedValue(new Blob(['synthetic'], { type: 'image/png' }))
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:history'), revokeObjectURL: vi.fn() }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const image = { image_id: 'image-one', name: 'synthetic.png', mime_type: 'image/png', width: 2, height: 3, url: '/api/v1/agent/sessions/s1/images/image-one' }

it('历史图片使用持久路由重新读取，点击看大图，Escape返回原焦点', async () => {
  render(<MessageImages images={[image]} />)
  const preview = screen.getByRole('button', { name: '预览图片：synthetic.png' })
  await waitFor(() => expect(preview).toBeEnabled())
  expect(api.read).toHaveBeenCalledWith(image.url, expect.any(AbortSignal))
  fireEvent.click(preview)
  expect(screen.getByRole('dialog', { name: '图片预览：synthetic.png' })).toBeInTheDocument()
  expect(screen.getByText('synthetic.png · 2 × 3')).toBeInTheDocument()
  fireEvent.keyDown(window, { key: 'Escape' })
  expect(screen.queryByRole('dialog')).toBeNull()
  expect(preview).toHaveFocus()
})

it('纯图片历史不重复显示占位字样，正文混合图片仍完整保留', async () => {
  const props = { actions: { onCopy: vi.fn() }, isLatest: true }
  const view = render(<UserMessage {...props} message={{ content: '[图片]', images: [image] } as never} />)
  expect(screen.queryByText('[图片]')).toBeNull()
  await waitFor(() => expect(screen.getByRole('img')).toHaveAttribute('src', 'blob:history'))
  view.rerender(<UserMessage {...props} message={{ content: '检查图中的表格', images: [image] } as never} />)
  expect(screen.getByText('检查图中的表格')).toBeInTheDocument()
})

import { act, cleanup, renderHook, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { useConversationImages } from '../lib/useConversationImages'
import type { AgentImageAttachment } from '../../api/agent'

const api = vi.hoisted(() => ({ upload: vi.fn() }))
vi.mock('../../api/agentImages', async (original) => ({ ...await original<typeof import('../../api/agentImages')>(), uploadAgentImage: api.upload }))
const a = { project: 'pa' as string | null, session: 'sa' as string | null, restoring: false }
const b = { project: null, session: 'sb', restoring: false }
const empty = { project: null, session: null, restoring: false }
const file = (name = 'sample.png') => new File(['synthetic'], name, { type: 'image/png' })
const meta = (name = 'sample.png'): AgentImageAttachment => ({ image_id: `image-${name}`, name, mime_type: 'image/png', width: 2, height: 2, url: `/api/v1/agent/images/image-${name}` })
function setup(scope = a) { return renderHook((props: typeof a) => useConversationImages(props.project, props.session, props.restoring), { initialProps: scope }) }
type Submission = ReturnType<ReturnType<typeof useConversationImages>['captureSubmission']>
beforeEach(() => {
  localStorage.clear(); vi.resetAllMocks()
  api.upload.mockImplementation(async (value: File) => meta(value.name))
  vi.stubGlobal('URL', Object.assign(URL, { createObjectURL: vi.fn(() => 'blob:preview'), revokeObjectURL: vi.fn() }))
})
afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('图片草稿与上传身份', () => {
  it('上传返回持久ID，存储只有metadata而没有图片二进制，刷新可恢复', async () => {
    const hook = setup()
    act(() => hook.result.current.add([file()]))
    await waitFor(() => expect(hook.result.current.images[0].status).toBe('ready'))
    expect(api.upload).toHaveBeenCalledOnce()
    const stored = localStorage.getItem('zhimeng.desk.composer-images:' + JSON.stringify(['pa', 'sa']))!
    expect(stored).toContain('image-sample.png')
    expect(stored).not.toMatch(/data:|blob:|synthetic|"file"/)
    hook.unmount()
    const refreshed = setup()
    expect(refreshed.result.current.images[0]).toMatchObject({ status: 'ready', image: meta() })
  })

  it('图片上传期间切B不串图，迟到结果仍记A；移除后迟到上传不复活图片', async () => {
    let finish!: (value: AgentImageAttachment) => void
    api.upload.mockReturnValueOnce(new Promise((resolve) => { finish = resolve }))
    const hook = setup()
    act(() => hook.result.current.add([file()]))
    const id = hook.result.current.images[0].clientId
    expect(hook.result.current.blocked).toBe(true)
    hook.rerender(b)
    expect(hook.result.current.images).toEqual([])
    await act(async () => finish(meta()))
    hook.rerender(a)
    expect(hook.result.current.images[0].status).toBe('ready')
    act(() => hook.result.current.remove(id))
    expect(hook.result.current.images).toEqual([])
    expect(URL.revokeObjectURL).toHaveBeenCalled()
  })

  it('失败上传显示原因并可重试，ready前不能捕获发送', async () => {
    api.upload.mockRejectedValueOnce(new Error('连接暂时中断'))
    const hook = setup()
    act(() => hook.result.current.add([file()]))
    await waitFor(() => expect(hook.result.current.images[0].status).toBe('failed'))
    expect(hook.result.current.images[0].error).toBe('连接暂时中断')
    expect(() => hook.result.current.captureSubmission()).toThrow('图片尚未上传完成')
    act(() => hook.result.current.retry(hook.result.current.images[0].clientId))
    await waitFor(() => expect(hook.result.current.blocked).toBe(false))
    expect(api.upload).toHaveBeenCalledTimes(2)
  })

  it('成功或未确认只清已发图片，新添加的图保留；首次创建会话可承接', async () => {
    const hook = setup(empty)
    act(() => hook.result.current.add([file('first.png')]))
    await waitFor(() => expect(hook.result.current.blocked).toBe(false))
    let submission!: Submission
    act(() => { submission = hook.result.current.captureSubmission() })
    expect(submission.imageIds).toEqual(['image-first.png'])
    act(() => hook.result.current.add([file('later.png')]))
    await waitFor(() => expect(hook.result.current.images.every((image) => image.status === 'ready')).toBe(true))
    hook.rerender(b)
    act(() => submission.retained())
    expect(hook.result.current.images.map((image) => image.name)).toEqual(['later.png'])
    hook.rerender(empty)
    expect(hook.result.current.images).toEqual([])
  })

  it('A明确发送失败保留图片，主动新对话不迁移；旧成功回调不清B的新图', async () => {
    const hook = setup()
    act(() => hook.result.current.add([file('a.png')]))
    await waitFor(() => expect(hook.result.current.blocked).toBe(false))
    let old!: Submission
    act(() => { old = hook.result.current.captureSubmission() })
    hook.rerender(b)
    act(() => hook.result.current.add([file('b.png')]))
    await waitFor(() => expect(hook.result.current.blocked).toBe(false))
    act(() => old.failed())
    expect(hook.result.current.images.map((image) => image.name)).toEqual(['b.png'])
    hook.rerender(a)
    expect(hook.result.current.images.map((image) => image.name)).toEqual(['a.png'])
  })

  it('刷新时尚未完成上传的本地图片照实提示重新添加，不伪造上传成功', () => {
    localStorage.setItem('zhimeng.desk.composer-images:' + JSON.stringify(['pa', 'sa']), JSON.stringify([{ clientId: 'local', name: 'pending.png', status: 'uploading' }]))
    const hook = setup()
    expect(hook.result.current.images[0]).toMatchObject({ status: 'failed', error: expect.stringContaining('重新添加') })
    expect(hook.result.current.blocked).toBe(true)
    act(() => hook.result.current.retry('local'))
    expect(api.upload).not.toHaveBeenCalled()
  })

  it('从历史编辑或未受理取回可复用同会话metadata，重复恢复不会重复挂图', () => {
    const hook = setup()
    act(() => hook.result.current.restore([meta()]))
    act(() => hook.result.current.restore([meta()]))
    expect(hook.result.current.images).toHaveLength(1)
    expect(hook.result.current.blocked).toBe(false)
  })
})

it('明确移动项目携带图片，目标移除不破坏原草稿的预览或上传身份', async () => {
  const hook = setup()
  act(() => hook.result.current.add([file()]))
  await waitFor(() => expect(hook.result.current.images[0].status).toBe('ready'))
  const original = hook.result.current.images[0].clientId
  const transfer = hook.result.current.captureTransfer()
  hook.rerender(b)
  act(() => transfer(b.project, b.session))
  expect(hook.result.current.images[0].image?.image_id).toBe(meta().image_id)
  expect(hook.result.current.images[0].clientId).not.toBe(original)
  act(() => hook.result.current.remove(hook.result.current.images[0].clientId))
  hook.rerender(a)
  expect(hook.result.current.images[0].clientId).toBe(original)
  expect(hook.result.current.images[0].status).toBe('ready')
})

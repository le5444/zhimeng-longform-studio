import { afterEach, describe, expect, it, vi } from 'vitest'
import { readAgentImage, uploadAgentImage } from './agentImages'

afterEach(() => vi.unstubAllGlobals())
describe('图片附件API', () => {
  it('实际PNG字节转dataURL上传，收到持久元数据', async () => {
    const bytes = Uint8Array.from(atob('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j6KsAAAAASUVORK5CYII='), (char) => char.charCodeAt(0))
    const file = new File([bytes], 'synthetic.png', { type: 'image/png' })
    const meta = { image_id: 'image-one', name: file.name, mime_type: file.type, width: 1, height: 1, url: '/api/v1/agent/images/image-one' }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(meta), { status: 201 }))
    await expect(uploadAgentImage(file, { fetcher, baseUrl: 'http://127.0.0.1:8788' })).resolves.toEqual(meta)
    const request = JSON.parse(fetcher.mock.calls[0][1]!.body as string)
    expect(request.name).toBe('synthetic.png')
    expect(request.data_url).toMatch(/^data:image\/png;base64,iVBOR/)
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8788/api/v1/agent/images')
  })

  it('图片读取只接受本机附件路由，外部URL不能收到认证头', async () => {
    const fetcher = vi.fn()
    vi.stubGlobal('fetch', fetcher)
    await expect(readAgentImage('https://external.example/picture.png', new AbortController().signal)).rejects.toThrow('本机附件记录')
    expect(fetcher).not.toHaveBeenCalled()
    fetcher.mockResolvedValue(new Response(new Blob(['synthetic'], { type: 'image/png' })))
    await expect(readAgentImage('/api/v1/agent/sessions/s1/images/image-one', new AbortController().signal)).resolves.toBeInstanceOf(Blob)
    expect(fetcher).toHaveBeenCalledOnce()
  })
})

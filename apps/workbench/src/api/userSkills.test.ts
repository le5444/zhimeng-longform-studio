import { describe, expect, it, vi } from 'vitest'
import { createUserSkill } from './userSkills'

describe('技能创建 API', () => {
  it('把 Markdown 正文与名称用途放在同一个创建请求中', async () => {
    const response = { ok: true, message: '已创建', skills: { skills: [] } }
    const fetcher = vi.fn<typeof fetch>().mockResolvedValue(new Response(JSON.stringify(response)))
    const content = '# 周报整理\n\n保留来源；用 `完成 / 进行中 / 待解决` 三组整理。\n'
    await expect(createUserSkill('周报整理', '整理工作记录', content, { fetcher, baseUrl: 'http://127.0.0.1:8788' })).resolves.toEqual(response)
    expect(fetcher).toHaveBeenCalledOnce()
    expect(fetcher.mock.calls[0][0]).toBe('http://127.0.0.1:8788/api/v1/user-skills')
    expect(JSON.parse(fetcher.mock.calls[0][1]!.body as string)).toEqual({ name: '周报整理', description: '整理工作记录', content })
  })
})

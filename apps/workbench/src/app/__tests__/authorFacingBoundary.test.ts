/**
 * 上游报错与作者提示边界测试。
 * 遵循作者铁律（2026-09-17 纠正）：
 * 上游返回什么错误，就如实返回什么错误（例如 502、429、401 等 HTTP 状态码与真实原因），
 * 直接把错误说出来，绝不遮遮掩掩。
 */
import { describe, expect, it } from 'vitest'
import { authorFacingAgentRunError, authorFacingError, authorFacingErrorText, formatUpstreamHttpError, stripHtmlToText } from '../../lib/authorFacingError'

describe('上游报错如实透传，绝不遮遮掩掩', () => {
  it('上游返回 502 时直接说出 502 与 Bad Gateway', () => {
    expect(formatUpstreamHttpError(502)).toContain('502')
    expect(formatUpstreamHttpError(502)).toContain('Bad Gateway')

    const text1 = authorFacingErrorText('HTTP 502 bad gateway')
    expect(text1).toContain('502')
    expect(text1).toContain('Bad Gateway')

    const text2 = authorFacingError(new Error('Provider returned HTTP 502: <html><title>502 Bad Gateway</title><body><center>cloudflare</center></body></html>'))
    expect(text2).toContain('502')
    expect(text2).toContain('Bad Gateway')
    // 不应留存 raw HTML 尖括号
    expect(text2).not.toContain('<html>')
  })

  it('上游返回 429 时直接说出 429 与配额限流原因', () => {
    const text = authorFacingErrorText('Provider returned HTTP 429: Rate limit reached: 3 requests per minute')
    expect(text).toContain('429')
    expect(text).toContain('Rate limit reached')
  })

  it('上游返回 401 时直接说出 401 与密钥/授权问题', () => {
    const text = authorFacingErrorText('Provider returned HTTP 401: Incorrect API key provided')
    expect(text).toContain('401')
    expect(text).toContain('Incorrect API key')
  })

  it('上游返回 503 / 504 / 520 等状态码如实呈现', () => {
    expect(authorFacingErrorText('HTTP 503 Service Unavailable')).toContain('503')
    expect(authorFacingErrorText('HTTP 504 Gateway Timeout')).toContain('504')
    expect(authorFacingErrorText('HTTP 524 origin timeout')).toContain('524')
  })

  it('带状态码的对象或 RuntimeApiError 直接识别并呈现', () => {
    const apiError = { status: 502, message: 'Provider returned HTTP 502: Bad Gateway' }
    const text = authorFacingError(apiError)
    expect(text).toContain('502')
    expect(text).toContain('Bad Gateway')
  })

  it('上游网络异常或超时直接说清原因', () => {
    expect(authorFacingErrorText('fetch failed: connect ECONNREFUSED 127.0.0.1:8787')).toContain('网络连接失败')
    expect(authorFacingErrorText('Provider stream timed out')).toContain('请求超时')
    expect(authorFacingErrorText('Provider request failed: peer closed connection')).toContain('peer closed connection')
  })

  it('自然表达与系统提示正常保留，不做机械术语替换', () => {
    expect(authorFacingErrorText('请先选择一个章节')).toBe('请先选择一个章节')
    expect(authorFacingErrorText('请先打开一本作品')).toBe('请先打开一本作品')
    expect(authorFacingErrorText('这次候选范围较大，请先看一眼')).toBe('这次候选范围较大，请先看一眼')
  })

  it('stripHtmlToText 能剥离 HTML 标签并保留核心文字', () => {
    expect(stripHtmlToText('<html><head><title>502 Bad Gateway</title></head><body><h1>Error</h1></body></html>')).toBe('502 Bad Gateway')
    expect(stripHtmlToText('<p>Rate limit exceeded</p>')).toBe('Rate limit exceeded')
  })

  it('authorFacingAgentRunError：上游任务失败真实呈现', () => {
    // 1. 上游 HTTP 错误
    const httpError = authorFacingAgentRunError('agent_provider_http_error', 'Provider returned HTTP 502: Bad Gateway')
    expect(httpError).toContain('502')
    expect(httpError).toContain('Bad Gateway')

    // 2. 模型名上报不一致
    const mismatch = authorFacingAgentRunError('agent_provider_model_mismatch', 'Provider reported "deepseek-v4-pro-0813" which was not accepted')
    expect(mismatch).toContain('deepseek-v4-pro-0813')

    // 3. 复读熔断
    const repeating = authorFacingAgentRunError('agent_provider_output_repeating', 'Model repeated "还了，门不开。" 12 times')
    expect(repeating).toContain('还了，门不开。')

    // 4. 工具超时
    const timeout = authorFacingAgentRunError('agent_tool_timeout', 'tool call timed out after 30s')
    expect(timeout).toContain('等太久')

    // 5. 流中断或截断
    const stream = authorFacingAgentRunError('agent_provider_incomplete_stream', 'stream ended unexpectedly')
    expect(stream).toContain('stream ended unexpectedly')
  })
})

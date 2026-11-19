import { describe, expect, it } from 'vitest'
import { authorFacingAgentRunError, authorFacingError, authorFacingErrorText } from './authorFacingError'

describe('「等太久」与「作者按了停止」要说得不一样', () => {
  it('超时：说清等了多久，并承认可能已经落盘，让作者先核对', () => {
    const timeout = Object.assign(new Error('等这台电脑上的写作服务回音，超过 30 秒没有等到'), {
      name: 'RuntimeTimeoutError',
      timeoutMs: 30_000,
    })
    const text = authorFacingError(timeout)
    expect(text).toContain('30 秒')
    expect(text).toContain('修改记录')
    expect(text).not.toContain('undefined')
  })

  it('作者自己掐断的：说「已经停下」，不算出错、也不催重试', () => {
    const cancelled = new DOMException('aborted', 'AbortError')
    const text = authorFacingError(cancelled)
    expect(text).toContain('已经停下')
    expect(text).not.toContain('超时')
  })
})

describe('authorFacingErrorText', () => {
  it('turns model transport failures into an actionable sentence with upstream status', () => {
    const text429 = authorFacingErrorText('HTTP 429: provider is in cooldown')
    expect(text429).toContain('429')
    expect(text429).toContain('provider is in cooldown')

    const text502 = authorFacingErrorText('upstream returned HTTP 502')
    expect(text502).toContain('502')
    expect(text502).toContain('Bad Gateway')

    expect(authorFacingErrorText('request timed out')).toContain('请求超时')
  })

  it('shows the sentence inside an upstream JSON error instead of the whole JSON', () => {
    // 2026-09-26 真机：换成 deepseek-v4-pro，失败卡上贴的是整段 JSON。
    const raw = 'Provider returned HTTP 400: {"error":{"message":"The request failed because it is missing `***.type` parameter. Request id: 0217","type":"invalid_request_error","param":"input.type","code":"MissingParameter"}}'
    const text = authorFacingAgentRunError('agent_provider_http_error', raw)
    expect(text).toContain('400')
    expect(text).toContain('The request failed because it is missing `***.type` parameter. Request id: 0217')
    expect(text).not.toContain('{"error"')
    // 解不开的（被截断）原样给，不吞掉
    expect(authorFacingErrorText('HTTP 400: {"error":{"message":"cut')).toContain('{"error":{"message":"cut')
  })

  it('preserves error context without artificial obscuring', () => {
    expect(authorFacingErrorText('agent_provider_http_error: session_id=abc')).toContain('session_id=abc')
  })

  it('keeps a short, useful ordinary-language error', () => {
    expect(authorFacingErrorText('请先选择一个章节')).toBe('请先选择一个章节')
    expect(authorFacingErrorText('')).toBe('这次没有完成，请稍后再试。')
  })
})

describe('authorFacingAgentRunError', () => {
  it('explains a model identity mismatch in plain language', () => {
    const text = authorFacingAgentRunError(
      'agent_provider_model_mismatch',
      'Provider reported a model identity that was not explicitly accepted',
    )
    expect(text).toContain('模型名字和设置对不上')
    expect(text).toContain('身份核对')
  })

  it('explains a silent model in plain words and keeps the upstream words', () => {
    const text = authorFacingAgentRunError('agent_provider_timeout', 'Provider stream timed out')
    expect(text).toContain('等待模型回复超时')
    expect(text).toContain('Provider stream timed out')
    expect(text).not.toContain('没有被改动')
  })

  it('uses network diagnostics when the exception message is empty', () => {
    const text = authorFacingAgentRunError('agent_provider_request_failed', 'Provider request failed: ', { network_error_type: 'ConnectError' })
    expect(text).toContain('连接模型服务失败')
    expect(text).toContain('ConnectError')
    expect(text).not.toContain('HTTP')
    expect(authorFacingAgentRunError('agent_provider_timeout', '', { timeout_kind: 'connect', elapsed_seconds: 12.4 })).toContain('连接模型服务超时，已等待 12 秒')
  })

  it('preserves upstream HTTP status and reason after backend humanization', () => {
    const text = authorFacingAgentRunError('agent_provider_http_error', '密钥被拒', { upstream_status: 401, upstream_excerpt: '{"error":{"message":"credential expired"}}' })
    expect(text).toContain('HTTP 401')
    expect(text).toContain('credential expired')
  })

  it('prefers the structured reason over a truncated diagnostic excerpt', () => {
    const text = authorFacingAgentRunError('agent_provider_http_error', 'Access denied', {
      upstream_status: 403, upstream_excerpt: '{"request_id":"truncated',
      upstream_message: 'Complete identity verification at https://account.example/security.',
    })
    expect(text).toContain('HTTP 403')
    expect(text).toContain('https://account.example/security')
    expect(text).not.toContain('request_id')
    expect(text).not.toContain('过期')
  })

  it.each(['bad gateway', 'invalid api key', '配额不足', 'cooldown', 'request id 15034'])('does not invent HTTP status from %s', (message) => {
    expect(authorFacingErrorText(message)).toBe(message)
  })

  it('explains a tool timeout without internal jargon', () => {
    expect(authorFacingAgentRunError('agent_tool_timeout', 'tool call timed out after 30s')).toContain('等太久')
  })

  it('explains a cut-off provider reply as an interrupted transfer', () => {
    for (const code of [
      'agent_provider_tool_arguments_invalid',
      'agent_provider_incomplete_stream',
      'agent_provider_output_truncated',
    ]) {
      const text = authorFacingAgentRunError(code, 'Provider cut the tool arguments short')
      expect(text).toContain('截断')
      expect(text).not.toContain('没有被改动')
    }
  })

  it('falls back to the ordinary-language mapping for unknown codes', () => {
    const text = authorFacingAgentRunError('agent_unknown_code', 'HTTP 429: rate limited')
    expect(text).toContain('429')
    expect(text).toContain('rate limited')

    expect(authorFacingAgentRunError(null, 'agent_x: session_id=abc')).toContain('session_id=abc')
  })
})

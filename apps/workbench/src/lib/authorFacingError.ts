/**
 * 把后台或上游服务的错误清晰、透明地呈现给作者。
 *
 * 遵循作者铁律（2026-09-17 纠正）：
 * 上游返回什么错误，就如实返回什么错误。比如说 502、429、401 等 HTTP 状态码与真实原因，
 * 直接把错误说出来，绝不遮遮掩掩，绝不能用笼统套话把底层真实报错吞掉。
 */

const DEFAULT_FALLBACK = '这次没有完成，请稍后再试。'

/** 剥离冗长无意义的 HTML 标签，提取出页面 title 或 h1 或紧凑纯文本，避免整屏 HTML 标签糊脸 */
export function stripHtmlToText(raw: string): string {
  let text = raw.trim()
  if (/<[a-z][\s\S]*>/i.test(text)) {
    const titleMatch = text.match(/<title>([\s\S]*?)<\/title>/i)
    const h1Match = text.match(/<h1>([\s\S]*?)<\/h1>/i)
    if (titleMatch && titleMatch[1].trim()) {
      text = titleMatch[1].trim()
    } else if (h1Match && h1Match[1].trim()) {
      text = h1Match[1].trim()
    } else {
      text = text
        .replace(/<style[\s\S]*?<\/style>/gi, '')
        .replace(/<script[\s\S]*?<\/script>/gi, '')
        .replace(/<[^>]+>/g, ' ')
        .replace(/\s+/g, ' ')
        .trim()
    }
  }
  return text
}

const HTTP_STATUS_LABELS: Record<number, string> = {
  400: 'Bad Request（请求参数错误）',
  401: 'Unauthorized（未授权 / API 密钥无效）',
  403: 'Forbidden（无权访问）',
  404: 'Not Found（未找到资源）',
  408: 'Request Timeout（请求超时）',
  409: 'Conflict（内容或状态已变化）',
  429: 'Too Many Requests（请求超限 / 配额限流）',
  500: 'Internal Server Error（服务内部错误）',
  502: 'Bad Gateway（上游网关错误）',
  503: 'Service Unavailable（服务不可用）',
  504: 'Gateway Timeout（网关超时）',
  520: '上游源站连接错误',
  522: '上游源站连接超时',
  524: '上游源站响应超时',
}

/**
 * 上游常把原因包在一段 JSON 里（`{"error":{"message":"…","param":"input.type",…}}`）：
 * 取出里面那句原话，不把整段 JSON 贴给作者（2026-09-26 真机：换模型失败，失败卡上是一长串 JSON，还撑破了卡片）。
 * 解不开（被截断、不是 JSON）就原样给。
 */
function upstreamJsonMessage(detail: string): string | null {
  const start = detail.indexOf('{')
  if (start < 0) return null
  try {
    const parsed = JSON.parse(detail.slice(start)) as { error?: unknown; message?: unknown }
    const nested = typeof parsed.error === 'object' && parsed.error !== null
      ? (parsed.error as { message?: unknown }).message
      : parsed.error
    const message = typeof nested === 'string' ? nested : parsed.message
    return typeof message === 'string' && message.trim() ? message.trim() : null
  } catch {
    return null
  }
}

/** 格式化上游 HTTP 状态码与详细原因 */
export function formatUpstreamHttpError(status: number, detailRaw?: string | null): string {
  const label = HTTP_STATUS_LABELS[status]
  const prefix = `HTTP ${status}${label ? ` ${label}` : ''}`
  const cleaned = detailRaw ? stripHtmlToText(detailRaw) : ''

  // 提取清洗后的详情，若已有冗余的前缀则去除
  let detail = cleaned
    .replace(/^Provider returned HTTP \d{3}:\s*/i, '')
    .replace(/^HTTP \d{3}(?:\s+[A-Za-z\s]+)?:\s*/i, '')
    .trim()
  detail = upstreamJsonMessage(detail) ?? detail

  // 如果详情内容和 label 里的英文一致（例如都是 Bad Gateway），就不再重复堆砌
  if (detail.toLowerCase() === 'bad gateway' && status === 502) {
    detail = ''
  }

  if (detail) {
    return `${prefix}: ${detail}`
  }
  return prefix
}

/**
 * 转换错误对象为真实、直接的错误提示。
 */
export function authorFacingError(error: unknown, fallback = DEFAULT_FALLBACK): string {
  if (error === null || error === undefined) return fallback

  if (typeof error === 'object') {
    const obj = error as { status?: unknown; message?: unknown; payload?: unknown; name?: unknown; timeoutMs?: unknown }
    // 「等太久」和「作者自己按了停止」以前到这一层已经长成同一个 AbortError，
    // 界面只能说一句含混的话。传输层现在把超时单独包了一个类型，这里分开讲。
    if (obj.name === 'RuntimeTimeoutError') return requestTimeoutText(obj.timeoutMs)
    if (obj.name === 'AbortError') return '已经停下了，这不是出错。'
    if (typeof obj.status === 'number' && obj.status >= 400) {
      const msg = typeof obj.message === 'string' ? obj.message : undefined
      return formatUpstreamHttpError(obj.status, msg)
    }
    if (typeof obj.message === 'string') {
      return authorFacingErrorText(obj.message, fallback)
    }
  }

  const raw = typeof error === 'string'
    ? error
    : error instanceof Error
      ? error.message
      : ''
  return authorFacingErrorText(raw, fallback)
}

/** 超时不能谎报成「没做成」：请求可能已经落盘，先让作者去核对再重试。 */
function requestTimeoutText(timeoutMs: unknown): string {
  const waited = typeof timeoutMs === 'number' && timeoutMs > 0
    ? `等了 ${Math.max(1, Math.round(timeoutMs / 1000))} 秒`
    : '等得比平时久'
  return `等这台电脑上的本机服务回音，${waited}还没等到，这一问就先掐断了。`
    + '它可能还在后台忙，也可能已经把文件改完了——先到右边的「修改记录」看一眼再决定要不要重试，别连着猛点。'
}

/**
 * 将错误文本直接、透明地转为作者提示，上游返回什么就呈现什么。
 */
export function authorFacingErrorText(raw: string | null | undefined, fallback = DEFAULT_FALLBACK): string {
  const value = raw?.trim()
  if (!value) return fallback

  // 1. 优先匹配明确的 HTTP 状态码（例如 Provider returned HTTP 502: ... 或 HTTP 429 ...）
  const statusMatch = value.match(/(?:(?:HTTP|status(?:\s*code)?|returned\s+HTTP)\s*[:=]?\s*|^HTTP\s+)(\d{3})/i)
  if (statusMatch) {
    const status = parseInt(statusMatch[1], 10)
    if (status >= 400 && status < 600) {
      return formatUpstreamHttpError(status, value)
    }
  }

  // Only an explicit HTTP status is evidence of an HTTP response.
  if (/timeout|timed out|aborted|等待太久|超时/i.test(value)) {
    const clean = stripHtmlToText(value)
    return `请求超时（Timeout）: ${clean}`
  }
  if (/failed to fetch|network|连接失败|无法连接|ECONNREFUSED|ERR_NETWORK|ERR_CONNECTION/i.test(value)) {
    const clean = stripHtmlToText(value)
    return `网络连接失败: ${clean}`
  }
  if (/未配置模型|没有配置.*模型|model.*not configured|provider.*not configured/i.test(value)) {
    return '还没有配置可用的模型服务，请先到设置里完成配置。'
  }

  // 3. 上游真实返回的内容，直接清洗 HTML 后如实呈现，绝不遮遮掩掩
  return stripHtmlToText(value)
}

/**
 * Agent 任务失败原因的作者话术。真实透明呈现上游报错。
 */
export function authorFacingAgentRunError(
  code: string | null | undefined,
  rawMessage: string | null | undefined,
  details?: Record<string, unknown> | null,
): string {
  if (typeof details?.upstream_status === 'number' && details.upstream_status >= 400 && details.upstream_status < 600) {
    return formatUpstreamHttpError(details.upstream_status,
      typeof details.upstream_message === 'string' && details.upstream_message
        ? details.upstream_message
        : typeof details.upstream_excerpt === 'string' ? details.upstream_excerpt : rawMessage)
  }
  if (code === 'agent_provider_request_failed') {
    const kind = typeof details?.network_error_type === 'string' ? details.network_error_type : ''
    const labels: Record<string, string> = {
      ConnectError: '连接模型服务失败',
      ReadError: '读取模型回复时连接中断',
      WriteError: '发送模型请求时连接中断',
      RemoteProtocolError: '模型服务返回的数据不符合接口协议',
      ProxyError: '连接代理服务失败',
    }
    const detail = rawMessage?.replace(/^Provider request failed:\s*/i, '').trim()
    const title = labels[kind] ?? '模型请求未能完成'
    return `${title}${kind ? `（${kind}）` : ''}${detail && detail !== kind ? `：${stripHtmlToText(detail)}` : '，未收到完整回复。'}`
  }
  if (code === 'agent_provider_model_mismatch') {
    const reported = rawMessage?.match(/"([^"]{1,120})"/)?.[1]
    if (reported) {
      return `模型服务这次把模型名报成了「${reported}」，和你设置里的名字对不上，所以这条回答没通过身份核对。如果这个名字是对的，到「设置 → 模型服务」把这个名字填进「接受的上报名」就行。`
    }
    return '模型服务这次上报的模型名字和设置对不上，这条回答没有通过系统的身份核对。如果每条都这样，请到「设置 → 模型服务」里检查模型配置。'
  }
  if (code === 'agent_provider_output_repeating') {
    const line = rawMessage?.match(/"([^"]{1,120})"/)?.[1]
    const quoted = line ? `（模型在反复说「${line}」）` : ''
    return `模型卡住了，开始翻来覆去说同一句话${quoted}，已经中止，没有浪费更多时间和额度。这通常是模型或线路当时的问题——可以直接重试，或到「设置 → 模型服务」换一个模型。`
  }
  if (code === 'agent_tool_timeout') {
    return '有一个步骤等太久被停下了。可以继续任务或查看已完成的操作。'
  }
  if (code === 'agent_provider_timeout') {
    // 2026-09-26 真机：中转站几分钟不回一个字，卡上只有一句英文「Provider stream timed out」。
    // 书动没动由卡片下一行按实际情况说，这里不替它下结论。
    const kind = details?.timeout_kind
    const label = kind === 'connect' ? '连接模型服务超时' : kind === 'write' ? '发送模型请求超时' : kind === 'pool' ? '等待可用连接超时' : '等待模型回复超时'
    const elapsed = typeof details?.elapsed_seconds === 'number' ? `，已等待 ${Math.round(details.elapsed_seconds)} 秒` : ''
    return `${label}${elapsed}。${rawMessage ? `（${rawMessage}）` : ''}`
  }
  if (code === 'agent_provider_tool_arguments_invalid') {
    return `模型通道把这次回答截断了（工具调用参数不是合法 JSON）${rawMessage ? `：${rawMessage}` : ''}`
  }
  if (
    code === 'agent_provider_incomplete_stream'
    || code === 'agent_provider_output_truncated'
  ) {
    return `模型通道把这次回答截断了${rawMessage ? `：${rawMessage}` : ''}`
  }

  return authorFacingErrorText(rawMessage, '这次任务没有正常收尾，请查看已完成的操作。')
}

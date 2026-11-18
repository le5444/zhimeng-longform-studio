import { ChevronRight } from 'lucide-react'
import type { AgentMessage } from '../../api/agent'

function tokenCount(value: unknown): number | null {
  return typeof value === 'number' && Number.isSafeInteger(value) && value >= 0 ? value : null
}

function reportedTokens(value: unknown, reported?: boolean, unit = true): string {
  const count = tokenCount(value)
  if (reported === false && (count === null || count === 0)) return '未完整上报'
  if (count === null) return '未报告'
  return `${unit ? count.toLocaleString('zh-CN') : count}${unit ? ' token' : ''}${reported === false ? '（部分上报）' : ''}`
}

function MessageUsage({ usage, includesCompaction }: { usage: NonNullable<AgentMessage['usage']>; includesCompaction: boolean }) {
  const input = tokenCount(usage.input_tokens)
  const cached = tokenCount(usage.cached_input_tokens)
  const inputComplete = usage.input_tokens_reported !== false
  const cacheComplete = usage.cached_input_tokens_complete !== false
  const percent = inputComplete && cacheComplete && input !== null && input > 0 && cached !== null && cached <= input ? Math.round(cached / input * 1000) / 10 : null
  const cacheLabel = !cacheComplete ? cached !== null && cached > 0 ? `缓存 ${cached.toLocaleString('zh-CN')} token（部分上报）` : '缓存未完整上报'
    : !inputComplete && cached !== null ? `缓存 ${cached.toLocaleString('zh-CN')} token（比例未知）`
      : percent !== null ? `缓存命中 ${percent}%`
    : usage.cached_input_tokens == null ? '缓存未报告'
      : input === 0 && cached === 0 ? '缓存 —' : '缓存数据异常'
  const cacheTitle = !cacheComplete ? '本轮有请求未报告缓存用量；保留已报告部分，无法计算完整命中比例'
    : !inputComplete ? '本轮输入用量未完整上报，无法计算完整缓存命中比例'
      : percent !== null ? `命中缓存 ${cached!.toLocaleString('zh-CN')} / 输入 ${input!.toLocaleString('zh-CN')} token`
    : input === 0 && cached === 0 ? '已报告输入为 0，无法计算缓存命中比例'
      : usage.cached_input_tokens == null ? '上游未完整报告本轮的缓存用量'
        : '上游报告的缓存用量无法计算有效比例，请查看请求详情'
  return <span className="zm-usage-inline" role="group" aria-label="本轮模型用量">
    <span className="zm-usage-scope" title="主任务与本轮工具结果整理的已报告用量；未报告部分不估算，token 数不等于实际账单。">本轮已报告{includesCompaction ? '（含工具整理）' : ''}</span>
    <span>输入 {reportedTokens(usage.input_tokens, usage.input_tokens_reported)}</span>
    <span>输出 {reportedTokens(usage.output_tokens, usage.output_tokens_reported)}</span>
    <span title={cacheTitle}>{cacheLabel}</span>
  </span>
}

/** Keep reported usage visible while request diagnostics remain expandable. */
export function MessageDetails({ usage: savedUsage, runUsage, parameterNotes }: {
  usage: AgentMessage['usage']; runUsage?: AgentMessage['run_usage']; parameterNotes: string[]
}) {
  const usage = runUsage ? runUsage.total : savedUsage
  if (!usage && !runUsage && parameterNotes.length === 0) return null
  const includesCompaction = Boolean(runUsage?.compaction.requests)
  const groups = runUsage ? [['主任务', runUsage.main], ['工具结果整理', runUsage.compaction]] as const : []
  return <details className="zm-request-details">
    <summary>
      {usage ? <MessageUsage usage={usage} includesCompaction={includesCompaction} /> : runUsage ? <span>本轮用量未报告</span> : null}
      <span className="zm-request-toggle"><ChevronRight aria-hidden /><span>请求详情</span></span>
    </summary>
    <div className="zm-request-body">
      {usage ? <dl>
        <dt>统计范围</dt><dd>{runUsage ? '主任务与本轮工具结果整理的已报告用量合计' : '主任务已保存的用量；旧记录未包含辅助请求'}</dd>
        <dt>输入 token</dt><dd>{reportedTokens(usage.input_tokens, usage.input_tokens_reported, false)}</dd>
        <dt>输出 token</dt><dd>{reportedTokens(usage.output_tokens, usage.output_tokens_reported, false)}</dd>
        {usage.cached_input_tokens != null || usage.cached_input_tokens_complete === false ? <><dt>命中缓存 token</dt><dd>{reportedTokens(usage.cached_input_tokens, usage.cached_input_tokens_complete, false)}</dd></> : null}
      </dl> : null}
      {groups.filter(([, group]) => group.requests > 0).map(([label, group]) => <section className="zm-usage-breakdown" key={label} aria-label={`${label}用量`}>
        <strong>{label}</strong>
        <dl>
          <dt>请求记录</dt><dd>{group.requests} 次，{group.completed} 次有完成回执</dd>
          <dt>输入 token</dt><dd>{reportedTokens(group.usage?.input_tokens, group.usage?.input_tokens_reported, false)}</dd>
          <dt>输出 token</dt><dd>{reportedTokens(group.usage?.output_tokens, group.usage?.output_tokens_reported, false)}</dd>
          <dt>命中缓存 token</dt><dd>{reportedTokens(group.usage?.cached_input_tokens, group.usage?.cached_input_tokens_complete, false)}</dd>
        </dl>
        {group.completed < group.requests ? <p>有请求尚无完成回执，用量可能不完整。</p> : null}
      </section>)}
      {runUsage ? <p>缓存是输入的一部分，不重复相加。未报告的用量不估算；此处不含标题生成、长期记忆等其他后台处理，也不代表实际账单。</p> : null}
      {parameterNotes.map((note) => <p key={note}>{note}</p>)}
    </div>
  </details>
}

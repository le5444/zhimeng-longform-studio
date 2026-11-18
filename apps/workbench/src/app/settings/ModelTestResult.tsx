import type { ProviderLivenessResult, ProviderLivenessStatus } from '../../api/providerSettings'
import { T } from '../strings'
import { wireName } from './modelForm'

function livenessTone(status: ProviderLivenessStatus): 'ok' | 'warn' | 'danger' {
  if (status === 'ok') return 'ok'
  if (status === 'timeout' || status === 'empty' || status === 'upstream') return 'warn'
  return 'danger'
}

/** 一个模型的测活结果：能不能出字、会不会调工具、走的哪种协议（自动时是挨个试出来的）。 */
export function LivenessDetail({ live }: { live: ProviderLivenessResult }) {
  const before = live.auto ? (live.tried ?? []).filter((item) => item.wire_format !== live.wire_format) : []
  const awaitingReply = live.status === 'ok' && !live.reply?.trim()
  return (
    <>
      <span className={`zm-chip is-${awaitingReply ? 'warn' : livenessTone(live.status)}`}>{awaitingReply ? '尚未收到正文' : T.settings.testResult[live.status] ?? live.label}</span>
      {live.usable && live.tools === 'ok' ? <span className="zm-chip is-ok">{T.settings.toolsOk}</span> : null}
      {live.usable && live.tools === 'no' ? <span className="zm-chip is-warn">{T.settings.toolsNo}</span> : null}
      {live.usable && live.tools === 'failed' ? <span className="zm-chip is-warn">{T.settings.toolsRoundtripFailed}</span> : null}
      {live.usable && live.tools === 'unsure' ? <span className="zm-chip is-warn">{T.settings.toolsUnsure}</span> : null}
      {live.usable && live.wire_format ? (
        <span className="zm-chip">{live.auto ? T.settings.routeAuto(wireName(live.wire_format)) : T.settings.routeChosen(wireName(live.wire_format))}</span>
      ) : null}
      {live.note ? <span>{live.note}</span> : null}
      {live.usable && (live.tools === 'no' || live.tools === 'failed' || live.tools === 'unsure') && live.tools_note ? <span>{live.tools_note}</span> : null}
      {live.usable && before.length > 0 ? (
        <span className="zm-faint">
          {T.settings.routeTriedBefore(before.map((item) => {
            const result = item.status !== 'ok'
              ? T.settings.testResult[item.status] ?? item.status
              : item.tools === 'tool_roundtrip_failed'
                ? T.settings.toolsRoundtripFailed
                : item.tools === 'no_tool_call'
                  ? T.settings.toolsNo
                  : T.settings.toolsUnsure
            return `${wireName(item.wire_format)}（${result}）`
          }).join('、'))}
        </span>
      ) : null}
    </>
  )
}

import { RefreshCw } from 'lucide-react'
import { T } from '../strings'
import { Button, Field, Input, SectionTitle, Working } from './kit'
import { LivenessDetail } from './ModelTestResult'
import { ST } from './settings.text'
import type { ModelServicesController } from './useModelServices'

export function ModelChecks({ state }: { state: ModelServicesController }) {
  const { form, summary, testPrompt, setTestPrompt, frozen, busy, testAll, retestAll, cancelTestAll, liveness, dirty } = state
  return <>
    <section className="zm-set-tests" data-setting="model-tests" tabIndex={-1}>
      <SectionTitle>验证回复与工具调用</SectionTitle>
      <p className="zm-faint">{ST.serviceDraftTests}</p>
      <div className="zm-settings-test-scope"><strong>{dirty ? '测试对象：当前未保存的连接' : '测试对象：当前服务连接'}</strong><span>{form.baseUrl || '尚未填写地址'} · {state.modelsToTest.length} 个模型</span><p>会向服务发送真实请求，可能产生费用。结果分别核对文字回复和工具往返；模型名称与身份真实性仍需独立确认。</p></div>
      <Field label={ST.testPromptLabel} hint={ST.testPromptHint}>
        <Input value={testPrompt} onChange={(event) => setTestPrompt(event.target.value)} placeholder={ST.testPromptPlaceholder} disabled={frozen} />
      </Field>
      <div className="zm-btn-row zm-set-test-actions">
        {busy === 'testingAll' ? (
          <>
            <Working>{T.settings.testing}</Working>
            <Button onClick={cancelTestAll}>{T.settings.testCancel}</Button>
          </>
        ) : (
          <>
            <Button onClick={() => void testAll()} disabled={frozen || state.modelsToTest.length === 0}>
              {T.settings.testAll}
            </Button>
            {/* 自动模式下加了重测：清空测活记忆从头来。固定协议的没路线可清，不显示。 */}
            {form.wireFormat === 'auto' ? (
              <Button variant="quiet" icon={<RefreshCw aria-hidden />} onClick={() => void retestAll()} disabled={frozen || state.modelsToTest.length === 0} title={T.settings.retestAllConfirm}>
                {T.settings.retestAll}
              </Button>
            ) : null}
          </>
        )}
        <span className="zm-faint">{T.settings.testHint}</span>
      </div>
      {summary ? (
        <p className="zm-set-summary">
          <strong>{T.settings.usableCount(summary.usable, summary.tested)}</strong>
          {summary.fastest ? <span>{T.settings.fastest(summary.fastest)}</span> : null}
        </p>
      ) : null}
      {Object.keys(liveness).length ? state.pane === 'tests' ? <ul className="zm-check-results">{Object.entries(liveness).map(([model, live]) => <li key={model}><strong>{model}</strong><div><LivenessDetail live={live} /></div></li>)}</ul> : null : <p className="zm-faint">尚无本次工具验证结果。可以先用上方的「测试连接」检查当前模型回复。</p>}
    </section>

  </>
}

import { Plus, Search, Settings2 } from 'lucide-react'
import { useId } from 'react'
import { T } from '../strings'
import { IconBtn } from '../ui'
import { Button, Input, SectionTitle, Select } from './kit'
import { wireName } from './modelForm'
import { LivenessDetail } from './ModelTestResult'
import { ST } from './settings.text'
import type { ModelServicesController } from './useModelServices'

export function ModelCatalog({ state }: { state: ModelServicesController }) {
  const { form, current, frozen, patch, selectAll, selectNone, modelQuery, setModelQuery, resultFilter, setResultFilter, onlySelected, setOnlySelected, visibleModels, verifiedCount, liveness, singleTests, connection, savedConnectionMatches, toggleModel, testOne, testingModel, dirty, setEditingModel, hasTestResults, manualModel, setManualModel, addManual } = state
  const radioName = useId()
  const manualId = useId()
  return <>
    <div className="zm-set-block" data-setting="model-list" tabIndex={-1}>
      <SectionTitle
        actions={(
          <>
            <Button small variant="quiet" onClick={selectAll} disabled={frozen || form.models.length === 0}>{T.settings.selectAll}</Button>
            <Button small variant="quiet" onClick={selectNone} disabled={frozen || form.selectedModels.length === 0}>{T.settings.selectNone}</Button>
          </>
        )}
      >
        {T.settings.selectedModels}
      </SectionTitle>
      <div className="zm-settings-filter">
        <label className="zm-settings-search"><Search aria-hidden /><Input value={modelQuery} onChange={(event) => setModelQuery(event.target.value)} placeholder="搜索模型" aria-label="搜索模型" /></label>
        <Select aria-label={ST.resultFilter} value={resultFilter} onChange={(event) => setResultFilter(event.target.value)}>
          <option value="all">{ST.allResults}</option>
          <option value="verified">{ST.toolsVerified}</option>
          <option value="pending">{ST.toolsNotTestedYet}</option>
          <option value="attention">{ST.needsAttention}</option>
        </Select>
        <label className="zm-check"><input type="checkbox" checked={onlySelected} onChange={(event) => setOnlySelected(event.target.checked)} />只看已选</label>
      </div>
      <div className="zm-set-list-caption"><span>{ST.visibleModels(visibleModels.length, form.models.length)}</span><span>{form.selectedModels.length} / {form.models.length} 已选</span><span title={ST.toolVerification}>{ST.verifiedModels(verifiedCount, form.selectedModels.length)}</span></div>

      {form.models.length === 0 ? (
        <p className="zm-faint">{ST.noModelsYet}</p>
      ) : (
        <ul className="zm-list zm-set-model-list">
          {visibleModels.map((model) => {
            const checked = form.selectedModels.includes(model)
            const live = state.pane === 'models' ? liveness[model] : undefined
            const single = state.pane === 'models' && singleTests[model]?.connection === connection ? singleTests[model].result : undefined
            const saved = savedConnectionMatches && form.wireFormat === 'auto' ? current?.verified_routes?.[model] : undefined
            const effective = savedConnectionMatches ? current?.effective_routes?.[model] : undefined
            return (
              <li key={model} className={`zm-set-model${checked ? ' is-selected' : ''}`}>
                <label className="zm-check zm-set-model-pick">
                  <input type="checkbox" checked={checked} onChange={() => toggleModel(model)} disabled={frozen} />
                  <span className="zm-set-model-name" title={model}>{model}</span>
                </label>
                {/* 自动模式下没测过的模型给「未测」提示：挪出 label，别干扰 checkbox 的 aria 名 */}
                {effective ? <span className="zm-set-route" title={effective.source === 'default' ? '按模型给出的接口建议，尚未在这条连接上验证' : effective.source === 'model_override' ? '此模型单独指定的接口' : effective.source === 'verified' ? '自动使用此前在这条连接上验证的接口' : '服务明确指定的接口'}>
                  {effective.source === 'default' ? `${wireName(effective.wire_format)} · 未验证` : effective.source === 'verified' ? `${wireName(effective.wire_format)} · 已验证线路` : effective.source === 'model_override' ? `${wireName(effective.wire_format)} · 模型例外` : `${wireName(effective.wire_format)} · 服务指定`}
                </span> : null}
                {form.wireFormat === 'auto' && !saved && !live && checked ? (
                  <span className="zm-faint" style={{ fontSize: 12, alignSelf: 'center' }}>{single?.ok ? ST.toolsNotTestedYet : T.settings.notTestedYet}</span>
                ) : null}
                {checked ? (
                  <label className="zm-check">
                    <input
                      type="radio"
                      name={radioName}
                      checked={form.defaultModel === model}
                      onChange={() => patch({ defaultModel: model })}
                      disabled={frozen}
                    />
                    <span>{T.settings.activeModel}</span>
                  </label>
                ) : null}
                {checked ? (
                  <Button
                    small
                    onClick={() => void testOne(model)}
                    busy={testingModel === model}
                    disabled={frozen || (testingModel !== null && testingModel !== model)}
                  >
                    {T.settings.testModel}
                  </Button>
                ) : null}
                <IconBtn label={`${model} 的连接设置`} title={dirty || !current ? '先保存服务设置' : '独立连接设置'}
                  disabled={frozen || dirty || !current || (!current.selected_models.includes(model) && !current.model_overrides?.[model])}
                  onClick={() => setEditingModel(model)} icon={<Settings2 />} />
                {live || single || saved ? (
                  <div className="zm-set-model-result">
                    {live ? <LivenessDetail live={live} /> : null}
                    {!live && !single && saved ? (
                      <span className="zm-faint">{T.settings.routeSaved(wireName(saved.wire_format), saved.tools)}</span>
                    ) : null}
                    {single ? (
                      single.ok
                        ? <><span className={`zm-chip ${single.replyReceived ? 'is-ok' : 'is-warn'}`}>{single.replyReceived ? ST.testOk(single.seconds) : ST.testNoReply(single.seconds)}</span>
                          {single.wireFormat ? <span className="zm-chip">{ST.textTestRoute(wireName(single.wireFormat))}</span> : null}
                          <span className="zm-faint">{single.replyReceived ? ST.textTestOnly : ST.textNotVerified}</span></>
                        : <p className="zm-set-test-error">{single.reason}</p>
                    ) : null}
                  </div>
                ) : null}
              </li>
            )
          })}
        </ul>
      )}
      {hasTestResults && dirty ? <p className="zm-faint">{ST.testedDraft}</p> : null}
      {form.models.length > 0 && visibleModels.length === 0 ? <div className="zm-set-no-results"><p>{ST.noMatchingModels}</p><Button small variant="quiet" onClick={() => { setModelQuery(''); setOnlySelected(false); setResultFilter('all') }}>{ST.clearModelFilters}</Button></div> : null}

      <label className="zm-set-manual-label" htmlFor={manualId}>{T.settings.manualModel}</label>
      <div className="zm-set-inline">
        <Input
          id={manualId}
          value={manualModel}
          onChange={(event) => setManualModel(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === 'Enter') {
              event.preventDefault()
              addManual()
            }
          }}
          placeholder={T.settings.manualModelPlaceholder}
          spellCheck={false}
          disabled={frozen}
        />
        <Button icon={<Plus aria-hidden />} onClick={addManual} disabled={frozen || !manualModel.trim()}>{T.settings.addManual}</Button>
      </div>

    </div>


  </>
}

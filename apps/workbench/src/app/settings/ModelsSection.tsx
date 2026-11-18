import { Plus, RotateCcw, Save, Server, Trash2 } from 'lucide-react'
import { T } from '../strings'
import { Button, Empty, Notice, Working } from './kit'
import { ModelAdvanced } from './ModelAdvanced'
import { ModelCatalog } from './ModelCatalog'
import { ModelChecks } from './ModelChecks'
import { ModelConnectionEditor } from './ModelConnectionEditor'
import { ModelConnectionFields } from './ModelConnectionFields'
import { modelDraftDifferences, wireName } from './modelForm'
import { SettingsConflict } from './SettingsConflict'
import { SettingsDisclosure } from './navigation'
import { ST } from './settings.text'
import { PROVIDER_PRESETS } from './providerPresets'
import { useModelServices, type ModelServicesTabProps } from './useModelServices'
import { DraftRecoveryNotice } from './DraftRecoveryNotice'

export function ModelServicesTab(props: ModelServicesTabProps) {
  const state = useModelServices(props)
  const { catalog, catalogState, selection, current, form, formOpen, frozen, dirty, busy, pane, setPane } = state
  const checkedModel = form.defaultModel.trim()
  const test = state.singleTests[checkedModel]
  const result = test?.connection === state.connection ? test.result : undefined
  return <>
    <DraftRecoveryNotice recovery={state.recovery} />
    {state.offline ? <Notice kind="danger">{T.runtime.offline}</Notice> : null}
    {state.editingModel && current ? <ModelConnectionEditor profile={current} model={state.editingModel}
      onClose={() => state.setEditingModel(null)} askConfirm={props.askConfirm} onSaved={state.modelOverrideSaved} /> : null}
    {!state.offline && props.provider.state === 'error' ? <Notice kind="warn" action={<Button small onClick={() => void props.provider.reload()}>{T.common.retry}</Button>}>当前模型设置读取失败，可以重试。</Notice> : null}
    <div className="zm-set-models">
      <aside className="zm-set-services" aria-label="模型服务列表">
        <div className="zm-set-service-heading"><h2>服务<small>{catalog?.providers.length ?? 0}</small></h2>
          <Button small variant="quiet" icon={<Plus />} active={selection?.kind === 'new'} onClick={() => state.requestOpen(null)} disabled={frozen}>{T.settings.addService}{state.hasDraft('new') ? ' · 未保存' : ''}</Button></div>
        {catalogState === 'loading' ? <Working>{T.common.loading}</Working> : null}
        {catalogState === 'error' ? <Notice kind="danger" action={<Button small onClick={() => void state.loadCatalog()} disabled={state.offline}>{T.common.retry}</Button>}>{state.catalogError || ST.servicesLoadFailed}</Notice> : null}
        {catalog?.providers.length === 0 ? <Empty title="添加第一个模型服务">填写 API 地址和密钥，再选择你要用的模型。</Empty> : null}
        <ul className="zm-list">{catalog?.providers.map(profile => <li key={profile.provider_id} className={`zm-set-service${selection?.kind === 'edit' && selection.id === profile.provider_id ? ' is-active' : ''}`}>
          <button type="button" className="zm-set-service-pick" aria-pressed={selection?.kind === 'edit' && selection.id === profile.provider_id}
            disabled={frozen} onClick={() => { if (selection?.kind !== 'edit' || selection.id !== profile.provider_id) state.requestOpen(profile) }}>
            <span className="zm-set-service-icon"><Server /></span><span className="zm-set-service-copy"><span className="zm-set-service-title"><span className="zm-set-service-name">{profile.name}</span>{profile.active ? <span className="zm-chip is-accent">{T.settings.inUse}</span> : null}</span>
              <span className="zm-set-service-sub" title={profile.base_url}>{profile.model || profile.base_url}</span>{state.hasDraft(profile.provider_id) ? <small>未保存</small> : null}</span>
          </button>
        </li>)}</ul>
        {state.orphanDrafts.map(draft => <button key={draft.id} type="button" className="zm-set-service-pick" disabled={frozen} onClick={() => state.requestOpen(draft.profile)}><span className="zm-set-service-copy"><span>{draft.name}</span><small>连接已移除 · 草稿保留</small></span></button>)}
      </aside>
      <section className="zm-set-form" aria-label="服务配置">
        {formOpen ? <>
          <div className="zm-set-form-title"><h2>{current?.name ?? ST.newServiceTitle}</h2><span className={`zm-save-state${dirty ? ' is-dirty' : ''}`}>{dirty ? '未保存' : current ? '已保存' : ''}</span></div>
          {current ? <div className="zm-settings-effect" aria-label="模型配置作用范围">
            <span>来源：{current.source === 'environment' ? '启动环境，保存后转为本机配置' : '本机保存的配置'}</span>
            <span>连接修改用于此服务的下一次请求；正在生成的回答继续完成。</span>
          </div> : null}
          {state.configurationChanged || state.removedElsewhere ? <SettingsConflict rows={modelDraftDifferences(form, state.latestProfile)}
            deleted={state.removedElsewhere} busy={frozen} onReload={state.discardStale} onKeep={state.mergeLatest} onCopy={state.copyRemoved} /> : null}
          {!current ? <div className="zm-settings-section"><div className="zm-provider-presets" role="group" aria-label="连接预设">{PROVIDER_PRESETS.map(preset => <Button small key={preset.name} disabled={frozen} onClick={() => state.applyPreset(preset.values)}>{preset.name}</Button>)}</div><p className="zm-faint">可选择预设填写常见地址，也可以直接填写下方字段。模型 ID 以服务实际提供的为准。</p></div> : null}
          <ModelConnectionFields state={state} />
          <div className="zm-btn-row zm-set-primary-actions">
            <Button variant="primary" icon={<Save />} onClick={() => void state.save()} busy={busy === 'saving'} disabled={frozen || state.configurationChanged || state.removedElsewhere}>{busy === 'saving' ? T.common.saving : T.settings.saveService}</Button>
            <Button onClick={() => void state.testOne(checkedModel)} busy={state.testingModel === checkedModel} disabled={frozen || !checkedModel || !form.baseUrl.trim()}>测试连接</Button>
            {dirty && !state.configurationChanged && !state.removedElsewhere ? <Button variant="quiet" icon={<RotateCcw />} disabled={frozen} onClick={() => state.openProfile(current)}>放弃修改</Button> : null}
          </div>
          {state.saveMessage ? <Notice kind={state.saveMessage.kind} role={state.saveMessage.kind === 'danger' ? 'alert' : 'status'}>{state.saveMessage.text}</Notice> : null}
          {result && pane !== 'models' ? <div className="zm-set-connection-result" role="status"><strong>{checkedModel}</strong>{result.ok ? <><span>{result.replyReceived ? ST.testOk(result.seconds) : ST.testNoReply(result.seconds)}</span>{result.wireFormat ? <span>{ST.textTestRoute(wireName(result.wireFormat))}</span> : null}<p className="zm-faint">{result.replyReceived ? ST.textTestOnly : ST.textNotVerified}</p></> : <p className="zm-set-test-error">{result.reason}</p>}</div> : null}
          <p className="zm-faint zm-set-default-scope">{current?.active ? '此服务用于新对话。' : current ? <Button small variant="quiet" disabled={frozen || dirty} onClick={() => void state.activate(current)}>{T.settings.useThis}</Button> : '第一个服务保存后用于新对话。'}已有对话在输入框旁切换模型。</p>
          <div className="zm-set-extras">
            <SettingsDisclosure id="model-catalog" title="管理可选模型" open={pane === 'models'} onToggle={() => setPane(pane === 'models' ? 'connection' : 'models')}><ModelCatalog state={state} /></SettingsDisclosure>
            <SettingsDisclosure id="model-tests" title="连接诊断" open={pane === 'tests'} onToggle={() => setPane(pane === 'tests' ? 'connection' : 'tests')}><ModelChecks state={state} /></SettingsDisclosure>
            <SettingsDisclosure id="model-advanced" title="更多设置" open={pane === 'advanced'} onToggle={() => setPane(pane === 'advanced' ? 'connection' : 'advanced')}><ModelAdvanced state={state} /></SettingsDisclosure>
          </div>
          {current ? <div className="zm-set-service-delete"><Button small variant="quiet" icon={<Trash2 />} onClick={() => state.confirmDelete(current)} disabled={frozen || state.configurationChanged || state.removedElsewhere}>{T.settings.deleteService}</Button></div> : null}
        </> : catalogState !== 'loading' ? <Empty>{ST.pickService}</Empty> : null}
      </section>
    </div>
  </>
}

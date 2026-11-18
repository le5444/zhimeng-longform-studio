import { Download, Eye, EyeOff } from 'lucide-react'
import { useId } from 'react'
import { T } from '../strings'
import { IconBtn } from '../ui'
import { Button, Field, Input, Notice } from './kit'
import type { ModelServicesController } from './useModelServices'

export function ModelConnectionFields({ state }: { state: ModelServicesController }) {
  const { form, patch, frozen, current, keyVisible, setKeyVisible, revealedKey, revealBusy, toggleRevealSavedKey } = state
  const keyId = useId()
  const modelsId = useId()
  return <>
    <div className="zm-set-connection" data-setting="connection" tabIndex={-1}>
      <div className="zm-set-connection-fields">
        <Field label={T.settings.baseUrl}>
          <Input
            value={form.baseUrl}
            onChange={(event) => patch({ baseUrl: event.target.value })}
            placeholder={T.settings.baseUrlPlaceholder}
            spellCheck={false}
            disabled={frozen}
          />
        </Field>
        <div className="zm-field">
          <label className="zm-field-label" htmlFor={keyId}>{T.settings.apiKey}</label>
          <div className="zm-set-secret">
            <Input
              id={keyId}
              type={keyVisible ? 'text' : 'password'}
              value={form.apiKey}
              onChange={(event) => patch({ apiKey: event.target.value })}
              placeholder={current?.api_key_set ? '已保存，留空保留原密钥' : '粘贴 API Key'}
              autoComplete="off"
              spellCheck={false}
              disabled={frozen}
            />
            <IconBtn
              label={keyVisible ? T.settings.hideKey : T.settings.showKey}
              icon={keyVisible ? <EyeOff aria-hidden /> : <Eye aria-hidden />}
              onClick={() => setKeyVisible((value) => !value)}
              aria-pressed={keyVisible}
              disabled={frozen || !form.apiKey}
            />
          </div>
          <small className="zm-field-hint" role="status">
            {form.apiKey.trim() ? T.settings.apiKeyDraft : current?.api_key_set ? T.settings.apiKeySet : T.settings.apiKeyUnset}
          </small>
          {!form.apiKey.trim() && current?.api_key_set && current.provider_id ? (
            <div className="zm-set-saved-key">
              <Button small variant="quiet" onClick={() => void toggleRevealSavedKey()} disabled={frozen || revealBusy} busy={revealBusy}>
                {revealedKey === null ? T.settings.showSavedKey : T.settings.hideSavedKey}
              </Button>
              {revealedKey !== null ? <code className="zm-set-revealed-key" data-testid="revealed-key">{revealedKey}</code> : null}
            </div>
          ) : null}
        </div>
        <div className="zm-set-primary-model">
          <Field label="模型" hint="选择已有模型，或直接填写服务提供的模型 ID。">
            <Input value={form.defaultModel} onChange={event => patch({ defaultModel: event.target.value })}
              list={modelsId} placeholder="例如 deepseek-chat" spellCheck={false} disabled={frozen} />
            <datalist id={modelsId}>{form.models.map(model => <option key={model} value={model} />)}</datalist>
          </Field>
          <Button small variant="quiet" icon={<Download aria-hidden />} onClick={() => void state.fetchModels()} busy={state.busy === 'fetching'} disabled={frozen}>
            {state.busy === 'fetching' ? T.settings.fetchingModels : T.settings.fetchModels}
          </Button>
        </div>
      </div>
      {state.currentDiscovery?.count !== undefined ? <p className="zm-faint" role="status">{T.settings.modelsFetched(state.currentDiscovery.count)}，在模型输入框中选择。</p> : null}
      {state.fetchWarn ? <Notice kind="warn" role="alert"><div>{state.fetchWarn}</div><div>可以直接填写模型 ID 后保存。</div></Notice> : null}
    </div>

  </>
}

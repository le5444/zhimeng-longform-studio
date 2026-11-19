import type { ProviderAuthType, ProviderWireFormat, ProviderReasoningEffort } from '../../api/providerSettings'
import { T } from '../strings'
import { Button, Field, Input, SectionTitle, Select, Textarea } from './kit'
import { AUTH_TYPES, WIRE_FORMATS } from './modelForm'
import type { ModelServicesController } from './useModelServices'

export function ModelAdvanced({ state }: { state: ModelServicesController }) {
  const { form, patch, frozen, savedHeaderNames, extraHeadersHint, effortValues } = state
  return <>
    <section className="zm-set-advanced" data-setting="model-advanced" tabIndex={-1}>
      <div className="zm-form-row">
        <Field label={T.settings.name} hint="留空时使用接口域名。"><Input aria-label={T.settings.name} value={form.name} onChange={event => patch({ name: event.target.value })} disabled={frozen} /></Field>
        <Field label={T.settings.wireFormat}><Select value={form.wireFormat} onChange={event => patch({ wireFormat: event.target.value as ProviderWireFormat })} disabled={frozen}>{WIRE_FORMATS.map(value => <option key={value} value={value}>{T.settings.wireFormats[value]}</option>)}</Select></Field>
      </div>
    <div className="zm-form-row zm-set-options" data-setting="model-options" tabIndex={-1}>
      <Field label={T.settings.effort} hint={T.settings.effortHint}>
        <Select
          value={form.reasoningEffort}
          onChange={(event) => patch({ reasoningEffort: event.target.value as ProviderReasoningEffort })}
          disabled={frozen}
        >
          {effortValues.map((value) => (
            <option key={value} value={value}>{T.settings.effortOptions[value]}</option>
          ))}
        </Select>
      </Field>

      <Field label={T.settings.contextWindow} hint={T.settings.contextWindowHint}>
        <Select
          value={form.contextWindow}
          onChange={(event) => patch({ contextWindow: event.target.value as '256k' | '1m' })}
          disabled={frozen}
        >
          {(['256k', '1m'] as const).map((value) => (
            <option key={value} value={value}>{T.settings.contextWindowOptions[value]}</option>
          ))}
        </Select>
      </Field>
    </div>
      <SectionTitle>高级连接选项</SectionTitle>
      <div className="zm-form-row">
        <Field label={T.settings.authType}>
          <Select
            value={form.authType}
            onChange={(event) => patch({ authType: event.target.value as ProviderAuthType })}
            disabled={frozen}
          >
            {AUTH_TYPES.map((value) => (
              <option key={value} value={value}>{T.settings.authTypes[value]}</option>
            ))}
          </Select>
        </Field>
      </div>
      {savedHeaderNames.length > 0 ? <ul className="zm-list" aria-label="已保存的请求头">
        {savedHeaderNames.map((name) => {
          const removed = form.removedHeaderNames.includes(name)
          return <li key={name} className="zm-btn-row">
            <code>{name}</code>
            {removed ? <span className="zm-faint">保存后移除</span> : null}
            <Button small variant="quiet" disabled={frozen} onClick={() => patch({ removedHeaderNames: removed ? form.removedHeaderNames.filter((item) => item !== name) : [...form.removedHeaderNames, name] })}>
              {removed ? `撤销移除 ${name}` : `移除 ${name}`}
            </Button>
          </li>
        })}
      </ul> : null}
      <Field label={T.settings.extraHeaders} hint={extraHeadersHint}>
        <Textarea
          rows={3}
          value={form.extraHeadersText}
          onChange={(event) => patch({ extraHeadersText: event.target.value })}
          placeholder={T.settings.extraHeadersPlaceholder}
          spellCheck={false}
          disabled={frozen}
        />
      </Field>
      <Field label={T.settings.modelsUrl} hint={T.settings.modelsUrlHint}>
        <Input
          value={form.modelsUrl}
          onChange={(event) => patch({ modelsUrl: event.target.value })}
          spellCheck={false}
          disabled={frozen}
        />
      </Field>
      <Field label={T.settings.acceptedNames} hint={T.settings.acceptedNamesHint}>
        <Textarea
          rows={3}
          value={form.acceptedText}
          onChange={(event) => patch({ acceptedText: event.target.value })}
          spellCheck={false}
          disabled={frozen}
        />
      </Field>
    </section>

  </>
}

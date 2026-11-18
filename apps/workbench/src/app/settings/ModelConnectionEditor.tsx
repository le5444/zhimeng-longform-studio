import { useEffect, useId, useRef, useState } from 'react'
import { RotateCcw, Save, X } from 'lucide-react'
import { patchProviderModelOverride, readProviderCatalog, type ProviderCatalogView, type ProviderModelOverridePatch, type ProviderProfileView } from '../../api/providerSettings'
import { RuntimeApiError } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { IconBtn, type AskConfirm } from '../ui'
import { Button, Field, Input, Notice, Select, Textarea } from './kit'
import { useSettingsDraft } from './drafts'
import { SettingsConflict } from './SettingsConflict'

const effortLabels = { auto: '自动', none: '关闭', low: '低', medium: '中', high: '高', xhigh: '极高', max: '最大', ultra: '超高' }

export function ModelConnectionEditor({ profile, model, onSaved, onClose, askConfirm }: {
  profile: ProviderProfileView
  model: string
  onSaved: (catalog: ProviderCatalogView) => void
  onClose: () => void
  askConfirm: AskConfirm
}) {
  const [baseProfile, setBaseProfile] = useState(profile)
  const [latest, setLatest] = useState<ProviderProfileView | null | undefined>(undefined)
  const saved = baseProfile.model_overrides?.[model]
  const [baseUrl, setBaseUrl] = useState(saved?.base_url ?? '')
  const [wire, setWire] = useState(saved?.wire_format ?? '')
  const [effort, setEffort] = useState(saved?.reasoning_effort ?? '')
  const [output, setOutput] = useState(saved?.max_output_tokens?.toString() ?? '')
  const [key, setKey] = useState('')
  const [inheritKey, setInheritKey] = useState(false)
  const [headers, setHeaders] = useState('')
  const [removed, setRemoved] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  const dialog = useRef<HTMLDialogElement>(null)
  const titleId = useId()
  const dirty = baseUrl !== (saved?.base_url ?? '') || wire !== (saved?.wire_format ?? '')
    || effort !== (saved?.reasoning_effort ?? '') || output !== (saved?.max_output_tokens?.toString() ?? '')
    || !!key || inheritKey || !!headers || removed.length > 0
  useSettingsDraft('model-connection', dirty)
  useEffect(() => { dialog.current?.showModal() }, [])

  function requestClose() {
    if (busy) return
    if (!dirty) { onClose(); return }
    askConfirm({
      title: '此模型的例外设置尚未保存',
      body: '关闭后将放弃这次修改，已保存的服务和模型连接不变。',
      confirmText: '放弃修改并关闭',
      onConfirm: onClose,
    })
  }

  function requestReset() {
    const apply = () => { void save(true) }
    if (!dirty) { apply(); return }
    askConfirm({
      title: '恢复使用服务连接？',
      body: '此模型已保存的独立地址、协议、密钥和请求头将被移除，改用服务连接。',
      confirmText: '全部沿用服务',
      onConfirm: apply,
    })
  }

  async function save(reset = false) {
    if (busy || latest !== undefined) return
    const extraHeaders: Record<string, string> = {}
    if (!reset) {
      for (const raw of headers.split('\n')) {
        if (!raw.trim()) continue
        const colon = raw.search(/[:：]/)
        if (colon <= 0 || !raw.slice(0, colon).trim()) {
          setError('请求头每行填写一个名字和值，用冒号分隔。')
          return
        }
        extraHeaders[raw.slice(0, colon).trim()] = raw.slice(colon + 1).trim()
      }
      if (output && (!Number.isSafeInteger(Number(output)) || Number(output) < 1 || Number(output) > 1000000)) {
        setError('输出长度应为 1 到 1000000 的整数。')
        return
      }
    }
    const patch: ProviderModelOverridePatch = reset ? { reset: true } : {
      base_url: baseUrl.trim() || null,
      wire_format: (wire || null) as ProviderModelOverridePatch['wire_format'],
      reasoning_effort: (effort || null) as ProviderModelOverridePatch['reasoning_effort'],
      max_output_tokens: output ? Number(output) : null,
      ...(key.trim() ? { api_key: key.trim() } : {}),
      inherit_api_key: inheritKey,
      extra_headers: extraHeaders,
      remove_header_names: removed,
    }
    if (baseProfile.revision) patch.expected_revision = baseProfile.revision
    setBusy(true)
    setError('')
    try {
      const catalog = await patchProviderModelOverride(profile.provider_id, model, patch)
      onSaved(catalog)
    } catch (reason) {
      setError(authorFacingError(reason, '模型连接保存失败'))
      if (reason instanceof RuntimeApiError && reason.status === 409) {
        try {
          const catalog = await readProviderCatalog()
          setLatest(catalog.providers.find(item => item.provider_id === profile.provider_id) ?? null)
        } catch { /* Keep the conflict and input; a later save can retry reading. */ }
      }
    } finally {
      setBusy(false)
    }
  }

  function resolveConflict(keep: boolean) {
    if (!latest) { onClose(); return }
    const next = latest.model_overrides?.[model]
    if (!keep || baseUrl === (saved?.base_url ?? '')) setBaseUrl(next?.base_url ?? '')
    if (!keep || wire === (saved?.wire_format ?? '')) setWire(next?.wire_format ?? '')
    if (!keep || effort === (saved?.reasoning_effort ?? '')) setEffort(next?.reasoning_effort ?? '')
    if (!keep || output === (saved?.max_output_tokens?.toString() ?? '')) setOutput(next?.max_output_tokens?.toString() ?? '')
    if (!keep) { setKey(''); setInheritKey(false); setHeaders(''); setRemoved([]) }
    setBaseProfile(latest)
    setLatest(undefined)
    setError('')
  }

  const comparison = latest?.model_overrides?.[model]
  const comparisonRows = [
    { label: '模型地址', saved: comparison?.base_url ?? '沿用服务', draft: baseUrl || '沿用服务' },
    { label: '接口协议', saved: comparison?.wire_format ?? '沿用服务', draft: wire || '沿用服务' },
    { label: '思考强度', saved: comparison?.reasoning_effort ?? '沿用服务', draft: effort || '沿用服务' },
    { label: '输出长度', saved: comparison?.max_output_tokens?.toString() ?? '沿用服务', draft: output || '沿用服务' },
  ].filter(row => row.saved !== row.draft)

  return (
    <dialog ref={dialog} className="zm-model-connection" aria-labelledby={titleId} onCancel={(event) => { event.preventDefault(); requestClose() }}>
      <form onSubmit={(event) => { event.preventDefault(); void save() }}>
        <div className="zm-model-connection-head"><h3 id={titleId}>{model} · 例外设置</h3><IconBtn label="关闭模型连接" disabled={busy} onClick={requestClose} icon={<X />} /></div>
        <p className="zm-faint">只覆盖这个模型的连接；留空的项目沿用服务设置。这里的修改不影响其他模型。</p>
        {latest !== undefined ? <SettingsConflict rows={comparisonRows} deleted={latest === null} busy={busy} onReload={() => resolveConflict(false)} onKeep={() => resolveConflict(true)} /> : null}
        <fieldset disabled={busy} className="zm-model-connection-fields">
          <Field label="模型地址"><Input value={baseUrl} onChange={(e) => setBaseUrl(e.target.value)} placeholder={profile.base_url ?? '沿用服务地址'} autoComplete="off" /></Field>
          <div className="zm-model-connection-grid">
            <Field label="接口协议"><Select aria-label="接口协议" value={wire} onChange={(e) => setWire(e.target.value as typeof wire)}><option value="">沿用服务设置</option><option value="responses">Responses</option><option value="chat">Chat Completions</option><option value="anthropic_messages">Anthropic Messages</option></Select></Field>
            <Field label="思考强度"><Select aria-label="模型思考强度" value={effort} onChange={(e) => setEffort(e.target.value as typeof effort)}><option value="">沿用服务设置</option>{Object.entries(effortLabels).map(([value, label]) => <option key={value} value={value}>{label}</option>)}</Select></Field>
          </div>
          <Field label="输出长度"><Input type="number" min={1} max={1000000} step={1} value={output} onChange={(e) => setOutput(e.target.value)} placeholder="沿用默认值" /></Field>
          <Field label="模型密钥"><Input type="password" autoComplete="new-password" value={key} onChange={(e) => setKey(e.target.value)} disabled={inheritKey} placeholder={saved?.api_key_set ? '已设置，留空保留' : '沿用服务密钥'} /></Field>
          {saved?.api_key_set ? <label className="zm-check"><input type="checkbox" checked={inheritKey} onChange={(e) => { setInheritKey(e.target.checked); setKey('') }} />恢复使用服务密钥</label> : null}
          <Field label="模型请求头"><Textarea value={headers} onChange={(e) => setHeaders(e.target.value)} rows={3} placeholder="Header-Name: value" /></Field>
          {(saved?.extra_header_names ?? []).map(name => <label key={name} className="zm-check"><input type="checkbox" checked={!removed.includes(name)} onChange={(e) => setRemoved(values => e.target.checked ? values.filter(v => v !== name) : [...values, name])} />{name}</label>)}
        </fieldset>
        {error ? <Notice kind="danger">{error}</Notice> : null}
        <div className="zm-model-connection-actions"><Button icon={<RotateCcw />} disabled={busy || !saved || latest !== undefined} onClick={requestReset}>全部沿用服务</Button><Button variant="quiet" disabled={busy} onClick={requestClose}>取消</Button><Button type="submit" variant="primary" icon={<Save />} busy={busy} disabled={!dirty || latest !== undefined}>保存例外设置</Button></div>
      </form>
    </dialog>
  )
}

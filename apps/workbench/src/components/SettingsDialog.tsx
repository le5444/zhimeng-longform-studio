import { useCallback, useEffect, useState } from 'react'
import { CircleCheck, Loader2, TriangleAlert, X } from 'lucide-react'
import {
  fetchProviderModels,
  readProviderSettings,
  saveProviderSettings,
  type ProviderSettingsView,
} from '../api/providerSettings'

interface SettingsDialogProps {
  open: boolean
  onClose: () => void
  onSaved?: (settings: ProviderSettingsView) => void
}

function message(error: unknown, fallback: string): string {
  return error instanceof Error && error.message ? error.message : fallback
}

export function SettingsDialog({ open, onClose, onSaved }: SettingsDialogProps) {
  const [baseUrl, setBaseUrl] = useState('')
  const [apiKey, setApiKey] = useState('')
  const [model, setModel] = useState('')
  const [models, setModels] = useState<string[]>([])
  const [current, setCurrent] = useState<ProviderSettingsView | null>(null)
  const [busy, setBusy] = useState<'none' | 'loading' | 'probing' | 'saving'>('none')
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    setBusy('loading')
    setError(null)
    setNotice(null)
    readProviderSettings()
      .then((settings) => {
        if (cancelled) return
        setCurrent(settings)
        setBaseUrl(settings.base_url)
        setModel(settings.model)
        setModels(settings.model ? [settings.model] : [])
        setApiKey('')
      })
      .catch((caught) => {
        if (!cancelled) setError(message(caught, '读取设置失败'))
      })
      .finally(() => {
        if (!cancelled) setBusy('none')
      })
    return () => {
      cancelled = true
    }
  }, [open])

  const probe = useCallback(async () => {
    if (!baseUrl.trim()) {
      setError('请先填写接口地址')
      return
    }
    setBusy('probing')
    setError(null)
    setNotice(null)
    try {
      const found = await fetchProviderModels(baseUrl.trim(), apiKey.trim() || undefined)
      setModels(found)
      setNotice(`拿到 ${found.length} 个模型。可以从建议中选择，也可以继续手填。`)
    } catch (caught) {
      setModels([])
      setError(message(caught, '获取模型失败'))
    } finally {
      setBusy('none')
    }
  }, [baseUrl, apiKey])

  const save = useCallback(async () => {
    if (!baseUrl.trim() || !model.trim()) {
      setError('接口地址和模型都要填。')
      return
    }
    setBusy('saving')
    setError(null)
    setNotice(null)
    try {
      const saved = await saveProviderSettings({
        baseUrl: baseUrl.trim(),
        model: model.trim(),
        // 留空表示不改动已保存的密钥
        apiKey: apiKey.trim() || undefined,
      })
      setCurrent(saved)
      setApiKey('')
      setNotice('已保存。续写和写作助手现在共用这套模型。')
      onSaved?.(saved)
    } catch (caught) {
      setError(message(caught, '保存失败'))
    } finally {
      setBusy('none')
    }
  }, [baseUrl, model, apiKey, onSaved])

  if (!open) return null

  const working = busy !== 'none'

  return (
    <div className="settings-backdrop" role="dialog" aria-modal="true" aria-label="写作模型设置">
      <div className="settings-dialog">
        <header className="settings-header">
          <h2>写作模型设置</h2>
          <button type="button" className="icon-button" aria-label="关闭" onClick={onClose}>
            <X size={17} />
          </button>
        </header>

        <p className="settings-intro">
          填好接口地址、密钥和模型就能保存。模型名可以直接手填；
          「获取模型」只是辅助查看，不是必经步骤，也不会消耗生成额度。
        </p>

        <label className="settings-label" htmlFor="settings-base-url">接口地址</label>
        <input
          id="settings-base-url"
          className="settings-input"
          type="text"
          value={baseUrl}
          placeholder="例如 https://api.example.com/v1"
          onChange={(event) => setBaseUrl(event.target.value)}
          disabled={working}
        />

        <label className="settings-label" htmlFor="settings-api-key">
          密钥
          {current?.api_key_set && (
            <span className="settings-hint">（已保存，留空表示不修改）</span>
          )}
        </label>
        <input
          id="settings-api-key"
          className="settings-input"
          type="password"
          value={apiKey}
          placeholder={current?.api_key_set ? '••••••••（留空则沿用已保存的）' : 'sk-...'}
          onChange={(event) => setApiKey(event.target.value)}
          disabled={working}
          autoComplete="off"
        />

        <div className="settings-model-row">
          <div className="settings-model-field">
            <label className="settings-label" htmlFor="settings-model">模型</label>
            <input
              id="settings-model"
              className="settings-input"
              type="text"
              list="settings-model-options"
              value={model}
              placeholder="可手填，例如 gpt-5.6-sol"
              onChange={(event) => setModel(event.target.value)}
              disabled={working}
            />
            <datalist id="settings-model-options">
              {models.map((name) => (
                <option key={name} value={name} />
              ))}
            </datalist>
          </div>
          <button
            type="button"
            className="settings-probe-button"
            onClick={() => void probe()}
            disabled={working}
          >
            {busy === 'probing' ? <Loader2 size={14} className="spin" /> : null}
            {busy === 'probing' ? '获取中' : '获取模型'}
          </button>
        </div>

        {error !== null && (
          <p className="settings-error" role="alert">
            <TriangleAlert size={14} /> {error}
          </p>
        )}
        {error === null && notice !== null && (
          <p className="settings-ok">
            <CircleCheck size={14} /> {notice}
          </p>
        )}

        <footer className="settings-footer">
          <span className="settings-state">
            {current?.configured
              ? `当前：${current.model}`
              : '当前还没配置好，「续写下一章」用不了'}
          </span>
          <div className="settings-actions">
            <button type="button" className="settings-cancel" onClick={onClose} disabled={working}>
              关闭
            </button>
            <button
              type="button"
              className="settings-save"
              onClick={() => void save()}
              disabled={working}
            >
              {busy === 'saving' ? '保存中…' : '保存'}
            </button>
          </div>
        </footer>
      </div>
    </div>
  )
}

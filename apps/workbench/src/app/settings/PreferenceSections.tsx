import { Check,Copy,RefreshCw,RotateCcw } from 'lucide-react'
import { useState } from 'react'
import { fetchRuntimeSnapshot,type RuntimeSnapshot } from '../../api/runtime'
import { readApplyPolicy,writeApplyPolicy,type ApplyPolicy } from '../../lib/applyPolicy'
import { useChatPreferences,writeChatPreferences } from '../lib/chatPreferences'
import { THEME_CHOICES,type ThemeChoice } from '../lib/theme'
import { DEFAULT_UI_SIZE,readUiSize,UI_SIZES,writeUiSize,type UiSize } from '../lib/uiSize'
import { T } from '../strings'
import { Btn,Switch,type Notify } from '../ui'
import { ST } from './settings.text'
import { S } from './settingsText'

function Row({ label, hint, children, id, saved }: { label: string; hint?: string; children: React.ReactNode; id?: string; saved?: boolean }) {
  return (
    <div className="zm-row" data-setting={id} tabIndex={id ? -1 : undefined}>
      <div className="zm-row-text">
        <span className="zm-row-label">{label}</span>
        {hint ? <span className="zm-row-hint">{hint}</span> : null}
        <SaveFeedback saved={saved} />
      </div>
      <div className="zm-row-control">{children}</div>
    </div>
  )
}

function Choice<K extends string>({ value, options, labels, onChange, label }: { value: K; options: readonly K[]; labels: Record<K, string>; onChange: (next: K) => void; label: string }) {
  return (
    <div className="zm-segment" role="group" aria-label={label}>
      {options.map((option) => (
        <button key={option} type="button" aria-pressed={value === option} className={value === option ? 'is-on' : ''} onClick={() => onChange(option)}>
          {labels[option]}
        </button>
      ))}
    </div>
  )
}

function SaveFeedback({ saved }: { saved: boolean | undefined }) {
  if (saved === undefined) return null
  return <span className={`zm-setting-feedback${saved ? '' : ' is-error'}`} role={saved ? 'status' : 'alert'}>
    {saved ? '已保存' : '保存失败，已保留原设置。请检查浏览器存储后重试。'}
  </span>
}

export function Appearance({ theme, onTheme }: { theme: ThemeChoice; onTheme: (theme: ThemeChoice) => boolean | void }) {
  const [size, setSize] = useState<UiSize>(readUiSize)
  const [saved, setSaved] = useState<Record<string, boolean>>({})
  const changeTheme = (next: ThemeChoice) => {
    const ok = onTheme(next) !== false
    setSaved((before) => ({ ...before, theme: ok }))
  }
  const changeSize = (next: UiSize) => {
    const ok = writeUiSize(next)
    if (ok) setSize(next)
    setSaved((before) => ({ ...before, size: ok }))
  }
  return (
    <section className="zm-settings-section" aria-label="外观">
      <div className="zm-settings-section-heading"><h2>外观</h2><Btn size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => { changeTheme('system'); changeSize(DEFAULT_UI_SIZE) }}>恢复默认外观</Btn></div>
      <div className="zm-setting-group">
        <div className="zm-appearance-themes" data-setting="theme" tabIndex={-1} role="group" aria-label={T.settings.theme}>
          {THEME_CHOICES.map(value => <button type="button" key={value} className={`zm-theme-choice is-${value}`} aria-pressed={theme === value} onClick={() => changeTheme(value)}>
            <span className="zm-theme-preview" aria-hidden><i /><span><b /><b /><em /></span></span>
            <span>{S.themes[value]}{theme === value ? <Check aria-hidden /> : null}</span>
          </button>)}
        </div>
        <SaveFeedback saved={saved.theme} />
        <Row id="ui-size" label={S.uiSize} hint="同时调整导航、对话和设置中的文字大小。" saved={saved.size}>
          <Choice label={S.uiSize} value={size} options={UI_SIZES} labels={S.uiSizes} onChange={changeSize} />
        </Row>
      </div>
    </section>
  )
}

export function Behavior() {
  const preferences = useChatPreferences()
  const [policy, setPolicy] = useState<ApplyPolicy>(readApplyPolicy)
  const [saved, setSaved] = useState<Record<string, boolean>>({})
  const changeDisplay = (field: 'expandThinking' | 'groupTools', value: boolean) => {
    const ok = writeChatPreferences({ [field]: value })
    setSaved((before) => ({ ...before, [field]: ok }))
  }
  const changePolicy = (next: ApplyPolicy) => {
    const ok = writeApplyPolicy(next)
    if (ok) setPolicy(next)
    setSaved((before) => ({ ...before, policy: ok }))
  }
  return <section className="zm-settings-section" aria-label="聊天偏好">
    <div className="zm-settings-section-heading"><h2>聊天偏好</h2><Btn size="sm" variant="ghost" icon={<RotateCcw />} onClick={() => {
      const ok = writeChatPreferences({ expandThinking: true, groupTools: true })
      setSaved((before) => ({ ...before, expandThinking: ok, groupTools: ok }))
      changePolicy('auto')
    }}>恢复默认工作方式</Btn></div>
    <div className="zm-setting-group">
    <Row id="thinking" label={S.behavior.thinking} hint="新回复到来时，直接显示模型提供的思考过程。" saved={saved.expandThinking}><Switch label={S.behavior.thinking} checked={preferences.expandThinking} onChange={() => changeDisplay('expandThinking', !preferences.expandThinking)} /></Row>
    <Row id="group-tools" label={S.behavior.groupTools} hint="连续的工具调用合并显示，可以随时展开详情。" saved={saved.groupTools}><Switch label={S.behavior.groupTools} checked={preferences.groupTools} onChange={() => changeDisplay('groupTools', !preferences.groupTools)} /></Row>
    <Row id="save-policy" label={ST.savePolicy} hint={ST.savePolicyNote} saved={saved.policy}>
      <Choice label={ST.savePolicy} value={policy} options={['auto', 'review'] as const} labels={{ auto: ST.savePolicyAuto, review: ST.savePolicyReview }} onChange={changePolicy} />
    </Row>
  </div></section>
}

export function About({ runtime, notify }: { runtime: RuntimeSnapshot; notify: Notify }) {
  const [checked, setChecked] = useState<RuntimeSnapshot | null>(null)
  const [busy, setBusy] = useState(false)
  const value = checked ?? runtime
  return (
    <>
      <p className="zm-intro">{T.settings.aboutIntro}</p>
      <div className="zm-card" data-setting="runtime" tabIndex={-1}>
        <Row label={T.settings.service}><span className="zm-row-value">{S.connection[value.connection]}</span></Row>
        <Row label={T.settings.version}><span className="zm-row-value">{value.version || T.common.none}</span></Row>
        <Row label="最近检查"><span className="zm-row-value">{value.checkedAt ? new Date(value.checkedAt).toLocaleString() : '尚未检查'}</span></Row>
        <div className="zm-btn-row">
          <Btn icon={<RefreshCw />} busy={busy} onClick={() => { setBusy(true); void fetchRuntimeSnapshot().then(setChecked).catch(() => notify('danger', '本机状态检查失败')).finally(() => setBusy(false)) }}>检查本机连接</Btn>
          <Btn variant="ghost" icon={<Copy />} onClick={() => void navigator.clipboard.writeText(JSON.stringify({ app: '织梦', version: value.version, connection: value.connection, ready: value.ready, checkedAt: value.checkedAt }, null, 2)).then(() => notify('ok', '已复制本机状态'), () => notify('danger', '复制没有成功'))}>复制诊断信息</Btn>
        </div>
      </div>
    </>
  )
}

import { useCallback,useEffect,useLayoutEffect,useMemo,useRef,useState } from 'react'
import {
activateProviderProfile,
clearVerifiedRoutes,
deleteProviderProfile,
fetchProviderModels,
readProviderCatalog,
revealProviderKey,
saveProviderProfile,
testProviderModel,
testProviderModelsLiveness,
type ProviderCatalogView,
type ProviderLivenessResult,
type ProviderProfileView
} from '../../api/providerSettings'
import { RuntimeApiError, type RuntimeSnapshot } from '../../api/runtime'
import { authorFacingError } from '../../lib/authorFacingError'
import { resolveEffortFamily } from '../../lib/effortOptions'
import { subscribeSettingsChanges } from '../../lib/settingsChanges'
import type { ProviderSettingsApi } from '../lib/useProviderSettings'
import { T } from '../strings'
import type { AskConfirm,Notify } from '../ui'
import { useSettingsDraft } from './drafts'
import { connectionSignature,formFromProfile,formSignature,mergeModelDraft,parseHeaders,splitLines,uniq,type Busy,type LivenessSummary,type Selection,type ServiceForm,type SingleTest } from './modelForm'
import { useSettingsPane } from './useSettingsPane'
import { ST } from './settings.text'
import { useDraftRecovery } from './draftRecovery'
import { parseModelDraftSnapshot, type ModelDraft, type ModelDraftSnapshot } from './modelDraftRecovery'

const MODEL_DESTINATIONS = { connection: 'connection', 'model-list': 'models', 'model-options': 'advanced', 'model-tests': 'tests', 'model-advanced': 'advanced' } as const

export interface ModelServicesTabProps {
  runtime: RuntimeSnapshot
  provider: ProviderSettingsApi
  onProviderChanged: () => Promise<void>
  notify: Notify
  askConfirm: AskConfirm
}

export function useModelServices({ runtime, provider: _provider, onProviderChanged, notify, askConfirm }: ModelServicesTabProps) {
  const recovery = useDraftRecovery('models', parseModelDraftSnapshot)
  const restored = recovery.initial
  const restoredKey = restored?.selection?.kind === 'edit' ? restored.selection.id : restored?.selection?.kind === 'new' ? 'new' : null
  const initialEntry = restored?.drafts.find(([id]) => id === restoredKey) ?? restored?.drafts[0]
  const initialDraft = initialEntry?.[1]
  const offline = runtime.connection === 'offline'
  const [pane, setPane] = useSettingsPane('connection', MODEL_DESTINATIONS)

  const [catalog, setCatalog] = useState<ProviderCatalogView | null>(null)
  const [catalogState, setCatalogState] = useState<'loading' | 'ready' | 'error'>('loading')
  const [catalogError, setCatalogError] = useState('')
  const catalogRequest = useRef(0)
  const [editingModel, setEditingModel] = useState<string | null>(null)
  const [selection, setSelection] = useState<Selection>(() => initialEntry ? initialEntry[0] === 'new' ? { kind: 'new' } : { kind: 'edit', id: initialEntry[0] } : null)
  const currentSelection = useRef(selection)
  currentSelection.current = selection
  const [form, setForm] = useState<ServiceForm>(() => initialDraft?.form ?? formFromProfile(null))
  const [baseline, setBaseline] = useState(() => initialDraft?.baseline ?? formSignature(formFromProfile(null)))
  const [baseProfile, setBaseProfile] = useState<ProviderProfileView | null>(initialDraft?.profile ?? null)
  const [saveMessage, setSaveMessage] = useState<{ kind: 'ok' | 'danger' | 'warn'; text: string } | null>(null)
  const drafts = useRef(new Map<string, ModelDraft>(restored?.drafts))
  const draftKey = selection?.kind === 'edit' ? selection.id : 'new'
  const [modelQuery, setModelQuery] = useState('')
  const [onlySelected, setOnlySelected] = useState(false)
  const [resultFilter, setResultFilter] = useState('all')
  const [manualModel, setManualModel] = useState('')
  const [testPrompt, setTestPrompt] = useState<string>(ST.testPrompt)
  const [busy, setBusy] = useState<Busy>('none')
  const [discovery, setDiscovery] = useState<{ connection: string; count?: number; error?: string } | null>(null)
  const [keyVisible, setKeyVisible] = useState(false)
  const [revealedKey, setRevealedKey] = useState<string | null>(null)
  const [revealBusy, setRevealBusy] = useState(false)
  const revealRequest = useRef(0)
  const [livenessResult, setLivenessResult] = useState<{
    connection: string; results: Record<string, ProviderLivenessResult>; summary: LivenessSummary
  } | null>(null)
  const [singleTests, setSingleTests] = useState<Record<string, { connection: string; result: SingleTest }>>({})
  const [testingModel, setTestingModel] = useState<string | null>(null)
  const abortRef = useRef<AbortController | null>(null)
  const pickedRef = useRef(Boolean(initialDraft))
  const pendingRecovery = useRef(Boolean(restored))
  const creationId = useRef(restored?.creationId ?? `provider-${crypto.randomUUID().replaceAll('-', '')}`)

  const latestProfile = useMemo(() => (
    selection?.kind === 'edit'
      ? catalog?.providers.find((profile) => profile.provider_id === selection.id) ?? null
      : null
  ), [catalog, selection])
  const current = useMemo(() => baseProfile ? { ...baseProfile, active: latestProfile?.active ?? false,
    ...(baseProfile.revision === latestProfile?.revision ? { verified_routes: latestProfile?.verified_routes, effective_routes: latestProfile?.effective_routes } : {}),
  } : null, [baseProfile, latestProfile])
  const formOpen = selection !== null
  const removedElsewhere = selection?.kind === 'edit' && catalogState === 'ready' && !latestProfile
  const configurationChanged = Boolean(baseProfile?.revision && latestProfile?.revision && baseProfile.revision !== latestProfile.revision)
  const frozen = offline || busy !== 'none' || testingModel !== null
  const dirty = formSignature(form) !== baseline
  const connection = connectionSignature(form, current?.provider_id)
  const savedConnectionMatches = current !== null && !configurationChanged && !removedElsewhere && connection === connectionSignature(formFromProfile(current), current.provider_id)
  const discoveryConnection = JSON.stringify([connection, form.modelsUrl.trim()])
  const currentDiscovery = discovery?.connection === discoveryConnection ? discovery : null
  const fetchWarn = currentDiscovery?.error
  const liveness = livenessResult?.connection === connection ? livenessResult.results : {}
  const summary = livenessResult?.connection === connection ? livenessResult.summary : null
  const hasTestResults = summary !== null || Object.values(singleTests).some((test) => test.connection === connection)
  const pendingDrafts = Array.from(drafts.current).filter(([id, draft]) => id !== draftKey && formSignature(draft.form) !== draft.baseline)
  const orphanDrafts = pendingDrafts.filter(([id, draft]) => id !== 'new' && draft.profile && catalogState === 'ready' && !catalog?.providers.some(profile => profile.provider_id === id))
  const dirtyProviders = [
    ...(dirty ? [form.name.trim() || current?.name || '新模型连接'] : []),
    ...pendingDrafts.map(([, draft]) => draft.form.name.trim() || '新模型连接'),
  ]
  useSettingsDraft('models', dirtyProviders.length > 0 || Boolean(recovery.error), dirtyProviders.join('、'))
  const persistedDrafts = new Map(pendingDrafts)
  if (dirty && formOpen) persistedDrafts.set(draftKey, { form, baseline, profile: baseProfile })
  const recoverySnapshot = JSON.stringify(persistedDrafts.size ? { selection, drafts: [...persistedDrafts], creationId: creationId.current } satisfies ModelDraftSnapshot : null)
  const persistDraft = recovery.persist
  useLayoutEffect(() => { persistDraft(JSON.parse(recoverySnapshot) as ModelDraftSnapshot | null) }, [persistDraft, recoverySnapshot])
  const modelResultState = (model: string) => {
    const live = liveness[model]
    if (live) return live.usable && live.tools === 'ok' ? 'verified' : 'attention'
    const single = singleTests[model]?.connection === connection ? singleTests[model].result : undefined
    if (single && !single.ok) return 'attention'
    const saved = savedConnectionMatches && form.wireFormat === 'auto' ? current?.verified_routes?.[model] : undefined
    return saved?.tools ? 'verified' : 'pending'
  }
  const verifiedCount = form.selectedModels.filter((model) => modelResultState(model) === 'verified').length
  const modelsToTest = uniq([...form.selectedModels, form.defaultModel])
  const visibleModels = form.models.filter((model) => model.toLocaleLowerCase().includes(modelQuery.trim().toLocaleLowerCase())
    && (!onlySelected || form.selectedModels.includes(model))
    && (resultFilter === 'all' || modelResultState(model) === resultFilter))

  const patch = useCallback((changes: Partial<ServiceForm>) => {
    setSaveMessage(null)
    if ('apiKey' in changes) { revealRequest.current++; setRevealedKey(null); setRevealBusy(false) }
    setForm((value) => ({ ...value, ...changes }))
  }, [])

  /** 显示/隐藏「已保存的密钥」：只在自己点了之后才去后端取明文。 */
  const toggleRevealSavedKey = useCallback(async () => {
    if (revealedKey !== null) { revealRequest.current++; setRevealedKey(null); return }
    if (!current?.provider_id) return
    const request = ++revealRequest.current
    setRevealBusy(true)
    try {
      const view = await revealProviderKey(current.provider_id)
      if (request === revealRequest.current) setRevealedKey(view.api_key)
    } catch (error) {
      if (request === revealRequest.current) notify('danger', authorFacingError(error, T.settings.revealKeyFailed))
    } finally {
      if (request === revealRequest.current) setRevealBusy(false)
    }
  }, [current?.provider_id, notify, revealedKey])

  /** 打开一套（null = 新的一套）：表单重填，测试结果清掉，正在跑的测试停掉。 */
  const openProfile = useCallback((profile: ProviderProfileView | null, restore = false) => {
    setSaveMessage(null)
    abortRef.current?.abort()
    revealRequest.current++
    setRevealBusy(false)
    setSelection(profile ? { kind: 'edit', id: profile.provider_id } : { kind: 'new' })
    const key = profile?.provider_id ?? 'new'
    const cached = restore ? drafts.current.get(key) : undefined
    if (!restore) drafts.current.delete(key)
    if (!profile && !restore) creationId.current = `provider-${crypto.randomUUID().replaceAll('-', '')}`
    setForm(cached?.form ?? formFromProfile(profile))
    setBaseline(cached?.baseline ?? formSignature(formFromProfile(profile)))
    setBaseProfile(cached?.profile ?? profile)
    setModelQuery('')
    setOnlySelected(false)
    setResultFilter('all')
    setManualModel('')
    setTestPrompt(ST.testPrompt)
    setDiscovery(null)
    setKeyVisible(false)
    setRevealedKey(null)
    setLivenessResult(null)
    setSingleTests({})
  }, [])

  const loadCatalog = useCallback(async (silent = false): Promise<ProviderCatalogView | null> => {
    const request = ++catalogRequest.current
    if (!silent) setCatalogState('loading')
    try {
      const found = await readProviderCatalog({ timeoutMs: 10_000 })
      if (request !== catalogRequest.current) return null
      setCatalog(found)
      setCatalogState('ready')
      setCatalogError('')
      if (pendingRecovery.current) {
        pendingRecovery.current = false
        const created = found.providers.find(profile => profile.provider_id === creationId.current)
        const draft = drafts.current.get('new')
        if (created && draft) {
          const recovered = { ...draft, profile: { ...created, revision: 'missing' } }
          drafts.current.delete('new')
          drafts.current.set(created.provider_id, recovered)
          if (currentSelection.current?.kind === 'new') {
            setSelection({ kind: 'edit', id: created.provider_id })
            setBaseProfile(recovered.profile)
            setSaveMessage({ kind: 'warn', text: '上次新建请求已找到对应连接，请核对最新配置与恢复的草稿。' })
          }
        }
      }
      if (!pickedRef.current) {
        pickedRef.current = true
        openProfile(found.providers.find((profile) => profile.active) ?? found.providers[0] ?? null)
      }
      return found
    } catch (error) {
      if (request === catalogRequest.current) {
        setCatalogState('error')
        setCatalogError(authorFacingError(error, ST.servicesLoadFailed))
      }
      return null
    }
  }, [openProfile])

  const requestOpen = (profile: ProviderProfileView | null) => {
    if (formOpen) {
      if (dirty) drafts.current.set(draftKey, { form, baseline, profile: baseProfile })
      else drafts.current.delete(draftKey)
    }
    openProfile(profile, true)
    setPane('connection')
  }

  useEffect(() => {
    if (!offline) void loadCatalog()
  }, [loadCatalog, offline])

  useEffect(() => {
    if (offline) return
    const refresh = () => { void loadCatalog(true) }
    const unsubscribe = subscribeSettingsChanges('providers', refresh)
    const timer = window.setInterval(() => { if (document.visibilityState !== 'hidden') refresh() }, 15_000)
    return () => { unsubscribe(); window.clearInterval(timer) }
  }, [loadCatalog, offline])

  useEffect(() => {
    if (busy !== 'none' || dirty || !configurationChanged || !latestProfile || editingModel) return
    setBaseProfile(latestProfile)
    setForm(formFromProfile(latestProfile))
    setBaseline(formSignature(formFromProfile(latestProfile)))
    setLivenessResult(null)
    setSingleTests({})
  }, [busy, configurationChanged, dirty, editingModel, latestProfile])

  useEffect(() => {
    // A revealed value or pending reveal belongs to one saved version.
    revealRequest.current++
    setRevealedKey(null)
    setRevealBusy(false)
  }, [latestProfile?.revision])

  useEffect(() => () => { abortRef.current?.abort(); revealRequest.current++; catalogRequest.current++ }, [])

  // ---- 模型列表的勾选 ----
  const toggleModel = useCallback((model: string) => {
    setForm((value) => {
      const existing = uniq([...value.selectedModels, value.defaultModel])
      const selected = existing.includes(model)
        ? existing.filter((item) => item !== model)
        : [...existing, model]
      return {
        ...value,
        selectedModels: selected,
        defaultModel: selected.includes(value.defaultModel) ? value.defaultModel : selected[0] ?? '',
      }
    })
  }, [])

  const selectAll = useCallback(() => {
    setForm((value) => ({
      ...value,
      selectedModels: uniq([...value.models, value.defaultModel]),
      defaultModel: value.defaultModel || value.models[0] || '',
    }))
  }, [])

  const selectNone = useCallback(() => {
    setForm((value) => ({ ...value, selectedModels: [], defaultModel: '' }))
  }, [])

  const addManual = useCallback(() => {
    const model = manualModel.trim()
    if (!model) return
    setForm((value) => ({
      ...value,
      models: uniq([...value.models, model]),
      selectedModels: uniq([...value.selectedModels, model]),
      defaultModel: value.defaultModel || model,
    }))
    setManualModel('')
  }, [manualModel])

  // ---- 获取模型 ----
  const fetchModels = useCallback(async () => {
    const baseUrl = form.baseUrl.trim()
    if (!baseUrl) {
      notify('danger', ST.needBaseUrl)
      return
    }
    const headers = parseHeaders(form.extraHeadersText)
    if (!headers.ok) {
      notify('danger', ST.extraHeadersBadLine(headers.line))
      return
    }
    setBusy('fetching')
    setDiscovery(null)
    try {
      const found = await fetchProviderModels(baseUrl, form.apiKey.trim() || undefined, {}, current?.provider_id, {
        authType: form.authType,
        extraHeaders: headers.value,
        ...(form.removedHeaderNames.length ? { removeHeaderNames: form.removedHeaderNames } : {}),
        modelsUrl: form.modelsUrl.trim(),
      })
      setForm((value) => ({ ...value, models: uniq([...value.models, ...found]) }))
      setDiscovery({ connection: discoveryConnection, count: found.length })
    } catch (error) {
      setDiscovery({ connection: discoveryConnection, error: authorFacingError(error, T.settings.fetchModelsFailed) })
    } finally {
      setBusy('none')
    }
  }, [discoveryConnection, current?.provider_id, form.apiKey, form.authType, form.baseUrl, form.extraHeadersText, form.removedHeaderNames, form.modelsUrl, notify])

  // ---- 测一个 ----
  const testOne = useCallback(async (model: string) => {
    const baseUrl = form.baseUrl.trim()
    if (!baseUrl) {
      notify('danger', ST.needBaseUrl)
      return
    }
    const headers = parseHeaders(form.extraHeadersText)
    if (!headers.ok) {
      notify('danger', ST.extraHeadersBadLine(headers.line))
      return
    }
    setTestingModel(model)
    try {
      const result = await testProviderModel({
        providerId: current?.provider_id,
        baseUrl,
        apiKey: form.apiKey.trim() || undefined,
        model,
        prompt: testPrompt.trim() || ST.testPrompt,
        wireFormat: form.wireFormat,
        authType: form.authType,
        extraHeaders: headers.value,
        ...(form.removedHeaderNames.length ? { removeHeaderNames: form.removedHeaderNames } : {}),
        acceptedReportedModels: splitLines(form.acceptedText),
      })
      const replyReceived = typeof result.reply === 'string' && result.reply.trim().length > 0
      setSingleTests((value) => ({ ...value, [model]: { connection, result: { ok: true, replyReceived, seconds: (result.latency_ms / 1000).toFixed(1), wireFormat: result.wire_format } } }))
    } catch (error) {
      setSingleTests((value) => ({ ...value, [model]: { connection, result: { ok: false, reason: authorFacingError(error, ST.testFailed) } } }))
    } finally {
      setTestingModel(null)
    }
  }, [connection, current?.provider_id, form.apiKey, form.baseUrl, form.wireFormat, form.authType, form.extraHeadersText, form.removedHeaderNames, form.acceptedText, notify, testPrompt])

  // ---- 测一批（可停止）：「自动」时后端挨个试协议、测通的记下来，所以测完重读一次列表 ----
  const runLiveness = useCallback(async (input: Parameters<typeof testProviderModelsLiveness>[0]) => {
    const controller = new AbortController()
    abortRef.current = controller
    setBusy('testingAll')
    try {
      const result = await testProviderModelsLiveness(input, { signal: controller.signal })
      setLivenessResult({ connection,
        results: Object.fromEntries(result.results.map((item) => [item.model, item])),
        summary: { usable: result.usable_count, tested: result.tested, fastest: result.fastest_model },
      })
      void loadCatalog(true)
    } catch (error) {
      if (controller.signal.aborted) notify('info', ST.testCancelled)
      else notify('danger', authorFacingError(error, ST.testAllFailed))
    } finally {
      if (abortRef.current === controller) abortRef.current = null
      setBusy('none')
    }
  }, [connection, loadCatalog, notify])

  // ---- 把勾选的都测一遍 ----
  const testAll = useCallback(async () => {
    const baseUrl = form.baseUrl.trim()
    if (!baseUrl) {
      notify('danger', ST.needBaseUrl)
      return
    }
    const models = uniq([...form.selectedModels, form.defaultModel])
    if (models.length === 0) {
      notify('danger', ST.needModelsToTest)
      return
    }
    const headers = parseHeaders(form.extraHeadersText)
    if (!headers.ok) {
      notify('danger', ST.extraHeadersBadLine(headers.line))
      return
    }
    await runLiveness({
      providerId: current?.provider_id,
      baseUrl,
      apiKey: form.apiKey.trim() || undefined,
      models,
      prompt: testPrompt.trim() || undefined,
      wireFormat: form.wireFormat,
      authType: form.authType,
      extraHeaders: headers.value,
      ...(form.removedHeaderNames.length ? { removeHeaderNames: form.removedHeaderNames } : {}),
      acceptedReportedModels: splitLines(form.acceptedText),
    })
  }, [current?.provider_id, form.apiKey, form.baseUrl, form.selectedModels, form.defaultModel, form.wireFormat, form.authType, form.extraHeadersText, form.removedHeaderNames, form.acceptedText, notify, runLiveness, testPrompt])

  const cancelTestAll = useCallback(() => {
    abortRef.current?.abort()
  }, [])

  // ---- 重测全部：清空这条线路的测活记忆，从头再测 ----
  const retestAll = useCallback(async () => {
    const baseUrl = form.baseUrl.trim()
    if (!baseUrl) {
      notify('danger', ST.needBaseUrl)
      return
    }
    const headers = parseHeaders(form.extraHeadersText)
    if (!headers.ok) {
      notify('danger', ST.extraHeadersBadLine(headers.line))
      return
    }
    askConfirm({
      title: T.settings.retestAll,
      body: T.settings.retestAllConfirm,
      confirmText: '重新验证',
      onConfirm: async () => {
        setBusy('testingAll')
        try {
          await clearVerifiedRoutes({
            baseUrl,
            apiKey: form.apiKey.trim() || undefined,
            providerId: current?.provider_id,
            authType: form.authType,
            extraHeaders: headers.value,
            ...(form.removedHeaderNames.length ? { removeHeaderNames: form.removedHeaderNames } : {}),
          })
          setLivenessResult(null)
          setSingleTests({})
          await testAll()
        } catch (error) {
          notify('danger', authorFacingError(error, ST.testAllFailed))
        } finally {
          setBusy('none')
        }
      },
    })
  }, [askConfirm, form.baseUrl, form.apiKey, form.authType, form.extraHeadersText, form.removedHeaderNames, current?.provider_id, notify, testAll])

  const reportCommitted = useCallback(async (message: string, listRefreshed = true) => {
    try {
      await onProviderChanged()
      setSaveMessage({ kind: listRefreshed ? 'ok' : 'warn', text: listRefreshed ? message : `${message} ${ST.savedListRefreshFailed}` })
      notify(listRefreshed ? 'ok' : 'warn', listRefreshed ? message : `${message} ${ST.savedListRefreshFailed}`)
    } catch (error) {
      setSaveMessage({ kind: 'warn', text: `${message} ${ST.savedRuntimeRefreshFailed}` })
      notify('warn', `${message} ${ST.savedRuntimeRefreshFailed} ${authorFacingError(error, ST.servicesLoadFailed)}`)
    }
  }, [notify, onProviderChanged])

  // ---- 保存 ----
  const save = useCallback(async () => {
    if (busy !== 'none') return
    if (configurationChanged || removedElsewhere) {
      setSaveMessage({ kind: 'warn', text: '请先核对最新配置与草稿，再保存。' })
      return
    }
    const baseUrl = form.baseUrl.trim()
    if (!baseUrl) {
      setPane('connection')
      setSaveMessage({ kind: 'danger', text: ST.needBaseUrl })
      notify('danger', ST.needBaseUrl)
      return
    }
    const model = form.defaultModel.trim()
    if (!model) {
      setPane('connection')
      setSaveMessage({ kind: 'danger', text: ST.needModels })
      notify('danger', ST.needModels)
      return
    }
    let name = form.name.trim()
    if (!name) {
      try { name = new URL(baseUrl).hostname } catch { /* API validates the address. */ }
      name ||= '自定义服务'
    }
    // A typed model becomes selectable only when submitted, never per keystroke.
    const selectedModels = uniq([...form.selectedModels, model])
    const headers = parseHeaders(form.extraHeadersText)
    if (!headers.ok) {
      setPane('advanced')
      setSaveMessage({ kind: 'danger', text: ST.extraHeadersBadLine(headers.line) })
      notify('danger', ST.extraHeadersBadLine(headers.line))
      return
    }
    // The backend makes the first connection default when its catalog is empty.
    const makeActive = false // Saving a connection never changes a separately chosen default.
    const apiKey = form.apiKey.trim()
    setBusy('saving')
    setSaveMessage(null)
    try {
      const result = await saveProviderProfile({
        providerId: current?.provider_id ?? creationId.current,
        ...(!current ? { expectedRevision: 'missing' } : baseProfile?.revision ? { expectedRevision: baseProfile.revision } : {}),
        name,
        baseUrl,
        model,
        selectedModels,
        ...(apiKey ? { apiKey } : {}),
        reasoningEffort: form.reasoningEffort,
        // 「自动」也照发：后端存成「没写死」，测活测通的协议才用得上；其余三种是作者明确选的。
        wireFormat: form.wireFormat,
        acceptedReportedModels: splitLines(form.acceptedText),
        authType: form.authType,
        ...(headers.value ? { extraHeaders: headers.value } : {}),
        ...(form.removedHeaderNames.length ? { removeHeaderNames: form.removedHeaderNames } : {}),
        modelsUrl: form.modelsUrl.trim(),
        contextWindow: form.contextWindow,
        makeActive,
      })
      // 保存的回应里就带着最新的列表，直接用，不再多跑一趟。
      catalogRequest.current++
      setCatalog(result.catalog)
      setCatalogState('ready')
      const saved = result.catalog.providers.find((profile) => profile.provider_id === result.profile.provider_id) ?? result.profile
      if (!current) creationId.current = `provider-${crypto.randomUUID().replaceAll('-', '')}`
      drafts.current.delete(draftKey)
      drafts.current.delete(saved.provider_id)
      // 保存成功：按后端返回的样子重填（密钥框清空、请求头只剩名字），表单里列过的模型留着。
      setSelection({ kind: 'edit', id: saved.provider_id })
      setBaseProfile(saved)
      setBaseline(formSignature(formFromProfile(saved)))
      setLivenessResult(null)
      setSingleTests({})
      setForm((value) => {
        const fresh = formFromProfile(saved)
        return { ...fresh, models: uniq([...value.models, ...fresh.models]) }
      })
      await reportCommitted(T.settings.saved)
    } catch (error) {
      if (!current || (error instanceof RuntimeApiError && error.status === 409)) {
        const found = await loadCatalog(true)
        const created = !current ? found?.providers.find(item => item.provider_id === creationId.current) : undefined
        if (created) {
          creationId.current = `provider-${crypto.randomUUID().replaceAll('-', '')}`
          setSelection({ kind: 'edit', id: created.provider_id })
          setBaseProfile({ ...created, revision: 'missing' })
          drafts.current.delete('new')
          setSaveMessage({ kind: 'warn', text: '已找到这份草稿对应的连接。上次请求的结果可能已保存，请核对最新配置后继续。' })
          return
        }
      }
      setSaveMessage({ kind: 'danger', text: authorFacingError(error, T.settings.saveServiceFailed) })
      notify('danger', authorFacingError(error, T.settings.saveServiceFailed))
    } finally {
      setBusy('none')
    }
  }, [baseProfile, busy, configurationChanged, current, draftKey, form, loadCatalog, notify, removedElsewhere, reportCommitted, setPane])

  // ---- 用这套 / 删除这套 ----
  const activate = useCallback(async (profile: ProviderProfileView) => {
    setBusy('activating')
    try {
      await activateProviderProfile(profile.provider_id, {}, profile.revision, catalog?.revision)
      const refreshed = await loadCatalog(true)
      await reportCommitted(ST.activated, refreshed !== null)
    } catch (error) {
      if (error instanceof RuntimeApiError && error.status === 409) await loadCatalog(true)
      notify('danger', authorFacingError(error, ST.activateFailed))
    } finally {
      setBusy('none')
    }
  }, [catalog?.revision, loadCatalog, notify, reportCommitted])

  const confirmDelete = useCallback((profile: ProviderProfileView) => {
    askConfirm({
      title: T.settings.deleteServiceConfirm(profile.name),
      body: T.settings.deleteServiceBody,
      confirmText: T.common.delete,
      danger: true,
      onConfirm: async () => {
        try {
          const next = await deleteProviderProfile(profile.provider_id, {}, profile.revision)
          catalogRequest.current++
          setCatalog(next)
          setCatalogState('ready')
          drafts.current.delete(profile.provider_id)
          if (selection?.kind === 'edit' && selection.id === profile.provider_id) {
            openProfile(next.providers.find((item) => item.active) ?? next.providers[0] ?? null, true)
          }
          // 非默认服务也可能正被某条对话使用，删除后同步聊天中的可选项和缺失状态。
          await reportCommitted(ST.deleted)
        } catch (error) {
          if (error instanceof RuntimeApiError && error.status === 409) await loadCatalog(true)
          throw new Error(authorFacingError(error, ST.deleteServiceFailed))
        }
      },
    })
  }, [askConfirm, loadCatalog, reportCommitted, openProfile, selection])

  // ---- 思考强度：按默认模型的家族给选项，已保存但不在家族里的值也留着 ----
  const effortValues = useMemo(() => {
    const values = resolveEffortFamily(form.defaultModel).efforts.map((item) => item.value)
    return values.includes(form.reasoningEffort) ? values : [...values, form.reasoningEffort]
  }, [form.defaultModel, form.reasoningEffort])

  const savedHeaderNames = current?.extra_header_names ?? []
  const extraHeadersHint = savedHeaderNames.length > 0
    ? `${T.settings.extraHeadersHint}${ST.savedHeaderNames(savedHeaderNames)}`
    : T.settings.extraHeadersHint

  return { offline, catalog, catalogState, catalogError, editingModel, setEditingModel, selection, form, current, formOpen, frozen, dirty, busy, pane, setPane, patch, requestOpen, openProfile, loadCatalog, activate, confirmDelete, save, modelQuery, setModelQuery, onlySelected, setOnlySelected, resultFilter, setResultFilter, visibleModels, manualModel, setManualModel, addManual, toggleModel, selectAll, selectNone, fetchModels, currentDiscovery, fetchWarn, connection, liveness, singleTests, savedConnectionMatches, verifiedCount, modelsToTest, hasTestResults, summary, testingModel, testOne, testPrompt, setTestPrompt, testAll, retestAll, cancelTestAll, keyVisible, setKeyVisible, revealedKey, revealBusy, toggleRevealSavedKey, effortValues, savedHeaderNames, extraHeadersHint,
    saveMessage, configurationChanged, removedElsewhere, latestProfile, recovery,
    orphanDrafts: orphanDrafts.map(([id, draft]) => ({ id, name: draft.form.name || draft.profile?.name || '已移除的连接', profile: draft.profile! })),
    mergeLatest: () => {
      if (!latestProfile) return
      const fresh = formFromProfile(latestProfile)
      setForm(mergeModelDraft(form, JSON.parse(baseline) as ServiceForm, fresh))
      setBaseProfile(latestProfile)
      setBaseline(formSignature(fresh))
      setSaveMessage({ kind: 'warn', text: '已合并最新配置与你修改的字段，请检查后保存。' })
      setLivenessResult(null)
      setSingleTests({})
    },
    discardStale: () => {
      drafts.current.delete(draftKey)
      openProfile(latestProfile ?? catalog?.providers[0] ?? null)
    },
    copyRemoved: () => {
      const copy = { ...form }
      drafts.current.delete(draftKey)
      openProfile(null)
      setForm(copy)
      setSaveMessage({ kind: 'warn', text: '已保留草稿为新连接；原服务的密钥不会自动复制，请重新填写。' })
    },
    applyPreset: (values: Partial<ServiceForm>) => {
      const apply = () => { openProfile(null); setForm({ ...formFromProfile(null), ...values }) }
      if (dirty) askConfirm({ title: '替换新连接草稿', body: '当前新连接的输入将被所选预设替换，密钥和请求头需要重新填写。', confirmText: '使用预设', onConfirm: apply })
      else apply()
    },
    hasDraft: (id: string) => id === draftKey ? dirty : pendingDrafts.some(([key]) => key === id),
    modelOverrideSaved: (next: ProviderCatalogView) => {
      catalogRequest.current++
      setCatalog(next)
      setCatalogState('ready')
      const saved = next.providers.find(profile => profile.provider_id === current?.provider_id)
      if (saved) {
        const fresh = formFromProfile(saved)
        setForm(value => mergeModelDraft(value, JSON.parse(baseline) as ServiceForm, fresh))
        setBaseline(formSignature(fresh))
        setBaseProfile(saved)
      }
      setLivenessResult(null)
      setSingleTests({})
      setEditingModel(null)
      void reportCommitted('模型连接已保存')
    },
  }
}

export type ModelServicesController = ReturnType<typeof useModelServices>

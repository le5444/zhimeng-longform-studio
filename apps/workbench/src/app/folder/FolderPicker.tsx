import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react'
import { ArrowUp, ChevronRight, Clock, Download, FileText, Folder, FolderOpen, FolderPlus, HardDrive, Home, Monitor, Star, X } from 'lucide-react'
import { listFolder, listPlaces, makeFolder, type FsListing, type FsPlace } from '../../api/fs'
import type { ProjectInspection, ScanProgress } from '../../api/projects'
import { authorFacingError } from '../../lib/authorFacingError'
import { Btn, IconBtn, Spinner, TextInput } from '../ui'
import { FT } from './folderText'
import './folder.css'

const PLACE_ICONS: Record<FsPlace['kind'], ReactNode> = {
  home: <Home aria-hidden />,
  desktop: <Monitor aria-hidden />,
  documents: <FileText aria-hidden />,
  downloads: <Download aria-hidden />,
  trusted: <Star aria-hidden />,
  recent: <Clock aria-hidden />,
  drive: <HardDrive aria-hidden />,
}

const INVALID_NAME_CHARACTERS = /[\\/:*?"<>|\u0000-\u001f]/
const RESERVED_NAMES = /^(con|prn|aux|nul|com[1-9]|lpt[1-9])$/i
const SCAN_LIMIT = /exceeds the .*(?:file|byte|directory|depth) limit/i

/** 名字当场查一遍（Windows 的规矩，再加「这里已经有了」）；没问题回 null。 */
export function folderNameProblem(name: string, siblings: readonly string[]): string | null {
  const trimmed = name.trim()
  if (trimmed === '') return null
  if (INVALID_NAME_CHARACTERS.test(trimmed)) return FT.nameBadChars
  if (trimmed.endsWith('.')) return FT.nameBadEnd
  if (RESERVED_NAMES.test(trimmed.split('.')[0])) return FT.nameReserved(trimmed)
  const key = trimmed.toLocaleLowerCase()
  if (siblings.some((sibling) => sibling.toLocaleLowerCase() === key)) return FT.nameTaken(trimmed)
  return null
}

function joinPath(parent: string, name: string): string {
  const separator = parent.includes('\\') || !parent.includes('/') ? '\\' : '/'
  return parent.endsWith(separator) ? `${parent}${name}` : `${parent}${separator}${name}`
}

/** 整个盘、主目录、Users：打开当项目要读的文件很多，提醒一句（不拦）。 */
function looksHuge(path: string, places: readonly FsPlace[]): boolean {
  if (/^[A-Za-z]:\\?$/.test(path) || path === '/') return true
  const key = path.toLocaleLowerCase()
  if (/^[a-z]:\\users\\?$/.test(key)) return true
  return places.some((place) => place.kind === 'home' && place.path.toLocaleLowerCase() === key)
}

function startingPlace(places: readonly FsPlace[]): string {
  return (places.find((place) => place.kind === 'trusted')
    ?? places.find((place) => place.kind === 'recent')
    ?? places.find((place) => place.kind === 'documents')
    ?? places.find((place) => place.kind === 'home')
    ?? places[0])?.path ?? ''
}

/**
 * 选文件夹（打开已有的 / 新建空白的）：浏览器自带的选框拿不到完整路径，框自己画、文件夹由后端列。
 * 左边常用位置，右边一层层点进去；「打开」把一个文件夹变成项目（左栏「项目」里就有它）。
 */
export function FolderPicker({ mode, openFolder, scanProgress, onOpened, onCancel }: {
  mode: 'open' | 'new'
  openFolder: (path: string) => Promise<ProjectInspection>
  scanProgress: ScanProgress | null
  onOpened: (projectId: string) => void
  onCancel: () => void
}) {
  const [places, setPlaces] = useState<FsPlace[]>([])
  const [listing, setListing] = useState<FsListing | null>(null)
  const [address, setAddress] = useState('')
  const [loading, setLoading] = useState(true)
  const [loadError, setLoadError] = useState<string | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [opening, setOpening] = useState<string | null>(null)
  const [name, setName] = useState('')
  const [newHere, setNewHere] = useState<string | null>(null)
  const [creating, setCreating] = useState(false)
  const requestSeq = useRef(0)
  const addressRevision = useRef(0)
  const addressTouched = useRef(false)

  /** keepTypedAddress：刚打开时跳去默认位置那一次，人已经在地址栏打了字就别盖掉。 */
  const load = useCallback(async (path: string, keepTypedAddress = false) => {
    const seq = ++requestSeq.current
    const revision = addressRevision.current
    setLoading(true)
    setLoadError(null)
    try {
      const next = await listFolder(path)
      if (seq !== requestSeq.current) return
      setListing(next)
      // Do not replace an address while the user is focusing, pasting or typing.
      // A late initial listing can otherwise move the caret into its own path.
      if (revision === addressRevision.current && !(keepTypedAddress && addressTouched.current)) setAddress(next.path)
      setNewHere(null)
    } catch (error) {
      if (seq === requestSeq.current) setLoadError(`${FT.loadFailed}${authorFacingError(error)}`)
    } finally {
      if (seq === requestSeq.current) setLoading(false)
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    // 常用位置回来之前人已经自己走了（粘了地址、点了哪一格）：就不再跳去默认位置，免得把人刚粘的地址盖掉。
    const userMoved = () => requestSeq.current > 0
    void listPlaces()
      .then((payload) => {
        if (cancelled) return
        setPlaces(payload.places)
        if (!userMoved()) void load(startingPlace(payload.places), true)
      })
      .catch(() => { if (!cancelled && !userMoved()) void load('', true) })
    return () => { cancelled = true }
  }, [load])

  const open = async (path: string) => {
    if (opening) return
    setOpening(path)
    setActionError(null)
    try {
      const inspection = await openFolder(path)
      onOpened(inspection.project.project_id)
    } catch (error) {
      const reason = authorFacingError(error)
      setActionError(`${FT.openFailed}${reason}${SCAN_LIMIT.test(reason) ? FT.tooBigHint : ''}`)
    } finally {
      setOpening(null)
    }
  }

  const siblings = listing?.entries.map((entry) => entry.name) ?? []
  const nameProblem = folderNameProblem(name, siblings)
  const newHereProblem = newHere !== null ? folderNameProblem(newHere, siblings) : null
  const busy = opening !== null || creating

  const createAndOpen = async (event: FormEvent) => {
    event.preventDefault()
    const trimmed = name.trim()
    if (!listing || !trimmed || nameProblem || busy) return
    setCreating(true)
    setActionError(null)
    try {
      const made = await makeFolder(listing.path, trimmed)
      setCreating(false)
      await open(made.path)
    } catch (error) {
      setActionError(`${FT.createFailed}${authorFacingError(error)}`)
      setCreating(false)
    }
  }
  const createHere = async (event: FormEvent) => {
    event.preventDefault()
    const trimmed = newHere?.trim() ?? ''
    if (!listing || !trimmed || newHereProblem || creating) return
    setCreating(true)
    setActionError(null)
    try {
      const made = await makeFolder(listing.path, trimmed)
      await load(made.path)
    } catch (error) {
      setActionError(`${FT.createFailed}${authorFacingError(error)}`)
    } finally {
      setCreating(false)
    }
  }

  return (
    <section className="zm-page zm-folder" aria-label={mode === 'open' ? FT.openTitle : FT.newTitle}>
      <header className="zm-page-head">
        <div>
          <h1>{mode === 'open' ? FT.openTitle : FT.newTitle}</h1>
          <p>{mode === 'open' ? FT.openIntro : FT.newIntro}</p>
        </div>
        <IconBtn label={FT.cancel} icon={<X />} onClick={onCancel} disabled={busy} />
      </header>
      <div className="zm-folder-grid">
        <nav className="zm-folder-places" aria-label={FT.places}>
          <p className="zm-folder-label">{FT.places}</p>
          {places.map((place) => (
            <button
              key={`${place.kind}:${place.path}`}
              type="button"
              className={`zm-folder-place${listing && listing.path.toLocaleLowerCase() === place.path.toLocaleLowerCase() ? ' is-current' : ''}`}
              title={`${FT.placeKinds[place.kind] ?? ''}：${place.path}`}
              onClick={() => void load(place.path)}
            >
              {PLACE_ICONS[place.kind] ?? <Folder aria-hidden />}
              <span>{place.label}</span>
            </button>
          ))}
        </nav>
        <div className="zm-folder-main">
          <form className="zm-folder-address" onSubmit={(event) => { event.preventDefault(); if (address.trim()) void load(address) }}>
            <IconBtn label={FT.up} icon={<ArrowUp />} disabled={!listing?.parent || loading} onClick={() => listing?.parent && void load(listing.parent)} />
            <TextInput aria-label={FT.address} value={address} placeholder={FT.addressPlaceholder} spellCheck={false}
              onFocus={() => { addressTouched.current = true; addressRevision.current += 1 }}
              onChange={(event) => { addressTouched.current = true; addressRevision.current += 1; setAddress(event.target.value) }} />
            <Btn size="sm" type="submit" disabled={!address.trim()}>{FT.go}</Btn>
          </form>
          {listing ? (
            <nav className="zm-folder-crumbs" aria-label={FT.address}>
              {listing.breadcrumbs.map((crumb, index) => (
                <span key={crumb.path}>
                  {index > 0 ? <ChevronRight aria-hidden /> : null}
                  <button type="button" disabled={index === listing.breadcrumbs.length - 1} onClick={() => void load(crumb.path)}>{crumb.name}</button>
                </span>
              ))}
            </nav>
          ) : null}
          {loadError ? <p className="zm-error-line">{loadError}</p> : null}
          <ul className="zm-folder-list" aria-busy={loading}>
            {loading && !listing ? <li className="zm-muted"><Spinner /> {FT.loading}</li> : null}
            {listing && listing.entries.length === 0 && !loading ? <li className="zm-muted">{FT.empty}</li> : null}
            {listing?.entries.map((entry) => (
              <li key={entry.path} className="zm-folder-row">
                <button type="button" className="zm-folder-entry" title={FT.enter(entry.name)} disabled={busy} onClick={() => void load(entry.path)}>
                  <Folder aria-hidden />
                  <span>{entry.name}</span>
                  {entry.is_project ? <span className="zm-chip is-ok">{FT.projectMark}</span> : null}
                </button>
                {mode === 'open' ? (
                  <Btn size="sm" variant="ghost" className="zm-folder-open" title={FT.openEntryHint(entry.name)} disabled={busy} busy={opening === entry.path} onClick={() => void open(entry.path)}>
                    {FT.openEntry}
                  </Btn>
                ) : null}
              </li>
            ))}
            {listing?.truncated ? <li className="zm-muted">{FT.truncated(listing.entries.length, listing.total)}</li> : null}
          </ul>
          {mode === 'open' && listing ? (
            newHere !== null ? (
              <form className="zm-folder-newhere" onSubmit={(event) => void createHere(event)}>
                <TextInput aria-label={FT.name} value={newHere} placeholder={FT.namePlaceholder} autoFocus onChange={(event) => setNewHere(event.target.value)} onKeyDown={(event) => { if (event.key === 'Escape') setNewHere(null) }} />
                <Btn size="sm" type="submit" busy={creating} disabled={!newHere.trim() || Boolean(newHereProblem)}>{FT.newHereCreate}</Btn>
                <Btn size="sm" variant="ghost" onClick={() => setNewHere(null)}>{FT.cancel}</Btn>
                {newHereProblem ? <small className="zm-folder-problem">{newHereProblem}</small> : null}
              </form>
            ) : (
              <Btn size="sm" variant="ghost" icon={<FolderPlus />} disabled={busy} onClick={() => setNewHere('')}>{FT.newHere}</Btn>
            )
          ) : null}
        </div>
      </div>
      <footer className="zm-folder-foot">
        {actionError ? <p className="zm-error-line">{actionError}</p> : null}
        {opening ? <p className="zm-muted"><Spinner /> {FT.scanning(scanProgress?.scanned ?? 0, scanProgress?.total ?? 0)}</p> : null}
        {mode === 'open' ? (
          <>
            {listing && looksHuge(listing.path, places) ? <p className="zm-warn-line">{FT.bigFolderHint}</p> : null}
            <div className="zm-folder-actions">
              <span className="zm-folder-current" title={listing?.path}>{listing ? FT.current(listing.path) : FT.loading}</span>
              <Btn variant="primary" icon={<FolderOpen />} disabled={!listing || loading || (busy && opening !== listing?.path)} busy={opening !== null && opening === listing?.path} onClick={() => listing && void open(listing.path)}>
                {FT.openThis}
              </Btn>
            </div>
          </>
        ) : (
          <form className="zm-folder-actions" onSubmit={(event) => void createAndOpen(event)}>
            <label className="zm-folder-name">
              <span>{FT.name}</span>
              <TextInput value={name} placeholder={FT.namePlaceholder} autoFocus aria-invalid={Boolean(nameProblem)} onChange={(event) => setName(event.target.value)} />
              <small>{nameProblem ?? (listing && name.trim() ? FT.willCreate(joinPath(listing.path, name.trim())) : '')}</small>
            </label>
            <Btn type="submit" variant="primary" icon={<FolderPlus />} disabled={!listing || !name.trim() || Boolean(nameProblem) || loading} busy={busy}>{FT.createAndOpen}</Btn>
          </form>
        )}
      </footer>
    </section>
  )
}

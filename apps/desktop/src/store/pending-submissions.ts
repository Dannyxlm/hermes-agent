import { JsonRpcGatewayError } from '@hermes/shared'

import type { ClientSessionState } from '@/app/types'
import type { SessionRuntimeInfo } from '@/types/hermes'

import { noteListedSubmissions } from './admitted-submissions'
import { getQueuedPrompts, type QueuedPromptEntry, queuedSubmissionId, writeSessionQueue } from './composer-queue'
import { recordSessionWriteVerdict } from './read-only-transcript'
import { $sessions, resolveComposerSessionKey } from './session'
import { captureSessionControlScope, sessionControlKey, sessionControlOwnerKey, type SessionControlScope } from './session-control-scope'
import type { SessionOwnerScope } from './session-request-router'

// One localStorage key per (session, admission). Every Desktop window of the origin shares this
// storage with last-writer-wins per KEY, so a single journal blob let one window's
// read-modify-write silently drop an entry another window had just tracked. Per-entry keys make
// each write touch only the record it owns.
const ENTRY_PREFIX = 'hermes.desktop.pendingSubmissions.v2:'
// The pre-v2 single blob, read for upgrade and drained per session as entries are rewritten.
const LEGACY_KEY = 'hermes.desktop.pendingSubmissions.v1'
interface PendingSubmission {
  id: string
  text: string
  displayText?: string
  status?: string
  /** Where in the session's event order the owner last listed this admission as pending. */
  seen?: PendingSnapshotFence
  /** Actual served controls, retained with their owner, never inferred from status. */
  controls?: string[]
  controlsOwnerKey?: string
  executionGeneration?: number
  observedUnknown?: QueuedPromptEntry['observedUnknown']
  /** This bridge's already-rendered input, not content read from observed-only metadata. */
  submittedInput?: Pick<QueuedPromptEntry, 'text' | 'displayText' | 'displayKind' | 'attachments' | 'submissionId' | 'queuedAt'>
  /** Presentation acknowledgement only, under the exact owner/lineage journal identity. */
  localReceiptAcknowledgement?: QueuedPromptEntry['queueReceipt']
}

/** A canonical pending snapshot's place in its session's event order: the replay epoch and the
 *  event sequence it reflects (a live `session.info` frame's own seq, or a resume snapshot's
 *  `last_sequence`, both read under the owner's event-stream lock). */
export interface PendingSnapshotFence {
  epoch: string
  sequence: number
}

const entryKey = (session: string, id: string) => `${ENTRY_PREFIX}${JSON.stringify([session, id])}`

const parse = <T>(raw: null | string, fallback: T): T => {
  try {
    return raw ? (JSON.parse(raw) as T) : fallback
  } catch {
    return fallback
  }
}

/** Every tracked/observed pending submission of one session, keyed by id. */
export function readPendingSubmissions(session: string): Record<string, PendingSubmission> {
  const entries: Record<string, PendingSubmission> = { ...parse(window.localStorage.getItem(LEGACY_KEY), {} as Record<string, Record<string, PendingSubmission>>)[session] }

  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index)

    if (!key?.startsWith(ENTRY_PREFIX)) { continue }
    const [owner, id] = parse(key.slice(ENTRY_PREFIX.length), [] as unknown[])
    const entry = parse<null | PendingSubmission>(window.localStorage.getItem(key), null)

    if (owner === session && typeof id === 'string' && entry) { entries[id] = entry }
  }

  return entries
}

/** Sessions the journal still holds an unsettled admission for (queued, started or unknown when
 *  last observed): the ones whose owner may have moved on while no window was watching. */
export function journaledPendingSessions(): string[] {
  const sessions = new Set<string>()
  const unsettled = (entry: null | PendingSubmission | undefined) => Boolean(entry?.status && entry.status !== 'retired' && !entry.observedUnknown)

  for (const [session, entries] of Object.entries(parse(window.localStorage.getItem(LEGACY_KEY), {} as Record<string, Record<string, PendingSubmission>>))) {
    if (Object.values(entries ?? {}).some(unsettled)) { sessions.add(session) }
  }

  for (let index = 0; index < window.localStorage.length; index += 1) {
    const key = window.localStorage.key(index)

    if (!key?.startsWith(ENTRY_PREFIX)) { continue }
    const [owner] = parse(key.slice(ENTRY_PREFIX.length), [] as unknown[])

    if (typeof owner === 'string' && unsettled(parse<null | PendingSubmission>(window.localStorage.getItem(key), null))) { sessions.add(owner) }
  }

  return [...sessions]
}

/** One admission's record without enumerating the journal: its v2 key, else the legacy blob. */
function readPendingSubmission(session: string, id: string): PendingSubmission | undefined {
  return parse<null | PendingSubmission>(window.localStorage.getItem(entryKey(session, id)), null) ??
    parse(window.localStorage.getItem(LEGACY_KEY), {} as Record<string, Record<string, PendingSubmission>>)[session]?.[id]
}

function writeEntries(session: string, before: Record<string, PendingSubmission>, after: Record<string, PendingSubmission>): void {
  for (const id of Object.keys(before)) {
    if (!(id in after)) { window.localStorage.removeItem(entryKey(session, id)) }
  }

  for (const [id, entry] of Object.entries(after)) {
    if (JSON.stringify(before[id]) !== JSON.stringify(entry)) { window.localStorage.setItem(entryKey(session, id), JSON.stringify(entry)) }
  }

  // Upgrade: once this session's entries live in v2 keys, drop its slice of the legacy blob.
  const legacy = parse(window.localStorage.getItem(LEGACY_KEY), {} as Record<string, unknown>)

  if (session in legacy) {
    for (const [id, entry] of Object.entries(after)) { window.localStorage.setItem(entryKey(session, id), JSON.stringify(entry)) }
    delete legacy[session]

    if (Object.keys(legacy).length) { window.localStorage.setItem(LEGACY_KEY, JSON.stringify(legacy)) }
    else { window.localStorage.removeItem(LEGACY_KEY) }
  }
}

// An admission only moves forward: queued -> started -> (unknown after an owner restart) ->
// retired (gone from the pending set: terminal). Snapshots can arrive out of order (a resume
// result racing the live fanout, a replayed session.info), so a receipt weaker than the strongest
// state already observed for its admission is stale and never repaints the queue.
const STATUS_RANK: Record<string, number> = { queued: 0, started: 1, unknown: 2, retired: 3 }

// The one journal identity for tracking and reconciliation: the key the queue panel reads. After
// compression that is the lineage root, not the tip a submit targets or the runtime reports.
const journalKey = (storedId: string) => resolveComposerSessionKey(storedId, $sessions.get()) ?? storedId

const rank = (entry: PendingSubmission) => STATUS_RANK[entry.status ?? ''] ?? -1

// Within one numbering the later sequence; across epochs they cannot be compared, keep the first.
const laterFence = (a?: PendingSnapshotFence, b?: PendingSnapshotFence) =>
  a && b && a.epoch === b.epoch && b.sequence > a.sequence ? b : (a ?? b)

/** Two records of one admission as one: the stronger status (the newer on a tie), the text and
 *  display text either knows, and the later fence. A retirement stays bare. */
function mergeReceipt(held: PendingSubmission | undefined, next: PendingSubmission): PendingSubmission {
  if (!held) {
    return next
  }

  const [strong, weak] = rank(next) >= rank(held) ? [next, held] : [held, next]

  if (strong.status === 'retired') {
    return strong
  }

  const seen = laterFence(strong.seen, weak.seen)

  return {
    ...weak,
    ...strong,
    text: strong.text || weak.text,
    displayText: strong.displayText ?? weak.displayText,
    ...(seen ? { seen } : {})
  }
}

/** Fold a journal kept under `from` into `to` once `from` resolves there (a tip journaled before
 *  its lineage row loaded), so its status, display text and fences keep guarding the card. */
function adoptJournal(from: string, to: string): void {
  const stray = from === to ? {} : readPendingSubmissions(from)

  if (!Object.keys(stray).length) {
    return
  }

  const before = readPendingSubmissions(to)
  const merged = { ...before }

  for (const [id, entry] of Object.entries(stray)) {
    merged[id] = mergeReceipt(merged[id], entry)
  }

  writeEntries(to, before, merged)
  writeEntries(from, stray, {})
}

// Tip->root lineages whose tip journal tracking already folded. Adoption enumerates the journal,
// so a send pays for it only when it first observes the lineage; reconciliation still adopts
// whatever another window journals under the tip afterwards.
const adoptedLineages = new Set<string>()

// Not an outbox: an uncertain accepted input must never be replayed automatically.
export function trackPendingSubmission(storedId: string, entry: PendingSubmission): void {
  const key = journalKey(storedId)
  const lineage = JSON.stringify([storedId, key])

  if (storedId !== key && !adoptedLineages.has(lineage)) {
    adoptJournal(storedId, key)
    adoptedLineages.add(lineage)
  }

  const held = readPendingSubmission(key, entry.id)
  window.localStorage.setItem(entryKey(key, entry.id), JSON.stringify(mergeReceipt(held, entry)))
}

// Array.isArray narrows `unknown` to `any[]`; the helpers keep that wire shape.
type RawReceipts = any[]

// Retired identities are remembered per session only as long as a late snapshot can matter.
const RETIRED_MEMORY = 200

const isStale = (raw: { admission_id: string; status: string }, known: Record<string, PendingSubmission>) =>
  (STATUS_RANK[raw.status] ?? 0) < (STATUS_RANK[known[raw.admission_id]?.status ?? ''] ?? -1)

function staleReceiptControls(entry: PendingSubmission, ownerKey: string): string[] {
  return entry.controlsOwnerKey === ownerKey ? entry.controls ?? [] : []
}

function collectReceipts(value: RawReceipts, known: Record<string, PendingSubmission>, ownerKey: string) {
  const receipts = new Map<string, PendingSubmission>()
  const admissionByInput = new Map<string, string>()

  for (const raw of value) {
    if (!raw || typeof raw.admission_id !== 'string' || !['queued', 'started', 'unknown'].includes(raw.status)) {
      continue
    }

    const id = raw.admission_id
    const stale = isStale(raw, known)

    // A stale status is still a listing: the admission is pending at its stronger observed state
    // (a replayed `started` keeps an `unknown` card and its Discard). A retired one stays retired.
    if (stale && known[id]?.status === 'retired') {
      continue
    }

    if (typeof raw.input_id === 'string') {
      admissionByInput.set(raw.input_id, id)
    }

    const status = stale ? known[id].status : raw.status
    const controls = stale ? staleReceiptControls(known[id], ownerKey) : raw.controls
    const executionGeneration = stale ? known[id].executionGeneration : raw.execution_generation

    receipts.set(id, {
      ...known[id],
      id,
      text: typeof raw.user === 'string' ? raw.user : (known[id]?.text ?? ''),
      status,
      executionGeneration: typeof executionGeneration === 'number' ? executionGeneration : undefined,
      controls: Array.isArray(controls) ? controls.filter((c: unknown) => typeof c === 'string') : undefined,
      controlsOwnerKey: ownerKey
    })
  }

  return { receipts, admissionByInput }
}

// Only a snapshot at least as new as the one that last listed an admission proves it left the
// pending set. An OLDER snapshot of the same numbering (a delayed resume answer, a replayed frame)
// predates it; its absence there says nothing. A different epoch is a later numbering (owner
// restart, evicted ring): sequences cannot be compared, and the current owner is authoritative.
// An unfenced snapshot (legacy shape) keeps the previous absence rule.
const predates = (fence: PendingSnapshotFence | undefined, seen: PendingSnapshotFence | undefined) =>
  Boolean(fence && seen && fence.epoch === seen.epoch && fence.sequence < seen.sequence)

function projectQueue(
  current: QueuedPromptEntry[],
  receipts: Map<string, PendingSubmission>,
  admissionByInput: Map<string, string>,
  stillPending: Set<string>
): QueuedPromptEntry[] {
  // The owner lists its admissions in FIFO order: a card it lists is shown in that order, so an
  // admission back in its queue place (a steer that never reached the turn) is not shown last.
  const order = new Map([...receipts.keys()].map((id, index) => [id, index]))
  const next: QueuedPromptEntry[] = []

  for (const entry of current) {
    if (entry.observedUnknown) { next.push(entry); receipts.delete(entry.id);

 continue }

    const submissionId = queuedSubmissionId(entry)
    const receipt = receipts.get(admissionByInput.get(submissionId) ?? submissionId)

    if (receipt) {
      if (receipt.status !== 'started' || receipt.controls?.includes('interrupt')) {
        next.push({ ...entry, id: receipt.id, submissionId, serverStatus: receipt.status, serverControls: receipt.controls, serverExecutionGeneration: receipt.executionGeneration })
      }

      receipts.delete(receipt.id)
    } else if (entry.observedUnknown || entry.queueReceipt || !entry.serverStatus || stillPending.has(entry.id)) {
      next.push(entry)
    }
  }

  for (const receipt of receipts.values()) {
    if (receipt.status !== 'started' || receipt.controls?.includes('interrupt')) {
      next.push({
        id: receipt.id,
        text: receipt.text,
        displayText: receipt.displayText,
        attachments: [],
        queuedAt: Date.now(),
        serverStatus: receipt.status,
        serverControls: receipt.controls,
        serverExecutionGeneration: receipt.executionGeneration
      })
    }
  }

  return inOwnerOrder(next, order)
}

/** Listed cards take the owner's order among the slots they hold; every other card keeps its slot. */
function inOwnerOrder(entries: QueuedPromptEntry[], order: Map<string, number>): QueuedPromptEntry[] {
  const slots = entries.flatMap((entry, index) => (order.has(entry.id) ? [index] : []))
  const listed = slots.map(index => entries[index]).sort((a, b) => (order.get(a.id) ?? 0) - (order.get(b.id) ?? 0))
  const next = [...entries]

  slots.forEach((slot, index) => {
    next[slot] = listed[index]
  })

  return next
}

function updateKnownReceipts(known: Record<string, PendingSubmission>, value: RawReceipts, ownerKey: string, fence?: PendingSnapshotFence): void {
  // Only observed server records may be retired by their later absence. The retirement is kept
  // (text dropped) so a stale snapshot that still lists the admission cannot resurrect its card.
  for (const [id, entry] of Object.entries(known)) {
    if (entry.status && entry.status !== 'retired' && !value.some(raw => raw?.admission_id === id) &&
        !predates(fence, entry.seen)) {
      known[id] = { id, text: '', status: 'retired' }
    }
  }

  const retired = Object.keys(known).filter(id => known[id].status === 'retired')

  for (const id of retired.slice(0, Math.max(0, retired.length - RETIRED_MEMORY))) {
    delete known[id]
  }

  for (const raw of value) {
    if (typeof raw?.admission_id === 'string' && !isStale(raw, known)) {
      const previous = known[raw.admission_id]

      known[raw.admission_id] = {
        ...previous,
        id: raw.admission_id,
        text: raw.user ?? previous?.text ?? '',
        // A steer in transfer reads `started` but can still go back to its queue place: it never
        // raises the admission's rank, so the owner's later `queued` listing repaints its card.
        status: raw.steering === true ? (previous?.status ?? 'queued') : raw.status,
        executionGeneration: typeof raw.execution_generation === 'number' ? raw.execution_generation : undefined,
        controls: Array.isArray(raw.controls) ? raw.controls.filter((control: unknown) => typeof control === 'string') : undefined,
        controlsOwnerKey: ownerKey,
        ...(fence && !predates(fence, previous?.seen) ? { seen: fence } : {})
      }
    }
  }
}

/** Project one pending snapshot onto the session's queue. `fence` orders canonical snapshots so a
 *  delayed older one cannot retire an admission a newer one still listed. */
export function reconcilePendingSubmissions(storedId: string, value: unknown, fence?: PendingSnapshotFence, scope = captureSessionControlScope(storedId)): void {
  if (!Array.isArray(value)) {
    return
  }

  // Project onto the key the queue panel reads: only snapshots projected there can retire a card
  // shown there. Receipts journaled under the tip before the lineage was known move along.
  const key = journalKey(storedId)
  adoptJournal(storedId, key)
  noteListedSubmissions(key, value.flatMap(raw => (typeof raw?.input_id === 'string' ? [raw.input_id] : [])))

  const before = readPendingSubmissions(key)
  const known = { ...before }
  const ownerKey = sessionControlOwnerKey(scope)
  // The owner-verified barrier slice (including revoked grants and terminal acknowledgements)
  // wins over the same admission's ordinary pending row, even in a stale concurrent resume.
  const observed = readPendingSubmissions(sessionControlKey(storedId, scope))
  const pending = value.filter(raw => !observed[raw?.admission_id]?.observedUnknown)
  const { receipts, admissionByInput } = collectReceipts(pending, known, ownerKey)

  const stillPending = new Set(Object.values(known).filter(entry =>
    entry.status && entry.status !== 'retired' && predates(fence, entry.seen) &&
    !value.some(raw => raw?.admission_id === entry.id)).map(entry => entry.id))

  const current = getQueuedPrompts(key)
  const next = projectQueue(current, receipts, admissionByInput, stillPending)

  updateKnownReceipts(known, pending, ownerKey, fence)
  writeEntries(key, before, known)

  if (JSON.stringify(current) !== JSON.stringify(next)) {
    writeSessionQueue(key, next)
  }
}

/** Only a served capability for the exact execution observed on the live stream may target Stop. */
export function pendingExecutionControl(storedId: string, live: ClientSessionState['streamExecutionControl']): { admission_id: string; execution_generation: number } | undefined {
  if (!live || live.ownerKey !== sessionControlOwnerKey(captureSessionControlScope(storedId))) { return undefined }
  const known = readPendingSubmission(journalKey(storedId), live.admission_id)

  return known?.status === 'started' && known.controlsOwnerKey === live.ownerKey && known.controls?.includes('interrupt') && known.executionGeneration === live.execution_generation
    ? { admission_id: live.admission_id, execution_generation: live.execution_generation } : undefined
}

/** Session-scoped control facts shared by primary and tiled successful resumes. */
export function reconcileSessionControlInfo(info: SessionRuntimeInfo, owner?: SessionOwnerScope, capturedScope?: SessionControlScope): void {
  if (!info.stored_session_id) { return }
  const scope = capturedScope ?? captureSessionControlScope(info.stored_session_id, owner)
  recordSessionWriteVerdict(info.stored_session_id, info.writable, scope)
  reconcileLocalQueueReceipts(info.stored_session_id, info.queue_receipts, scope)
  reconcilePendingSubmissions(info.stored_session_id, info.pending_submissions,
    pendingSnapshotFence(info.replay_epoch, info.last_sequence), scope)
  reconcileBlockingUnknown(info.stored_session_id, info.blocking_unknown, scope, pendingSnapshotFence(info.replay_epoch, info.last_sequence), true, info)
}

/** Reuse the bounded retired journal, not a new profile-keyed persistence family or server verb. */
export function acknowledgeLocalQueueReceipt(entry: QueuedPromptEntry): void {
  const receipt = entry.queueReceipt

  if (!receipt?.ownerKey || receipt.status === 'queued') { return }
  const before = readPendingSubmissions(receipt.ownerKey)
  const after = { ...before }
  delete after[entry.id]
  after[entry.id] = { id: entry.id, text: '', status: 'retired', localReceiptAcknowledgement: receipt }
  const retained = Object.fromEntries(Object.entries(after).slice(-RETIRED_MEMORY))
  writeEntries(receipt.ownerKey, before, retained)
}

function isLocalQueueReceiptAcknowledged(id: string, receipt: NonNullable<QueuedPromptEntry['queueReceipt']>): boolean {
  if (!receipt.ownerKey) { return false }
  const acknowledged = readPendingSubmission(receipt.ownerKey, id)?.localReceiptAcknowledgement

  return Boolean(acknowledged && (receipt.status === 'queued' || JSON.stringify(acknowledged) === JSON.stringify(receipt)))
}

function localQueueReceiptMatches(raw: RawReceipts[number], key: string): boolean {
  return Boolean(raw && typeof raw.input_id === 'string' && typeof raw.session_id === 'string' &&
    journalKey(raw.session_id) === key && ['queued', 'transferred', 'terminal', 'unknown'].includes(raw.status))
}

/** Durable local-queue dispositions are observations, never an outbox to drain. */
export function reconcileLocalQueueReceipts(storedId: string, value: unknown, scope: SessionControlScope = captureSessionControlScope(storedId)): void {
  if (!Array.isArray(value)) { return }
  const key = journalKey(storedId)
  const ownerKey = sessionControlKey(storedId, scope)
  let next = getQueuedPrompts(key)

  for (const raw of value) {
    if (!localQueueReceiptMatches(raw, key)) { continue }

    const id = `local-queue:${raw.input_id}`
    const held = next.find(entry => entry.id === id)
    next = next.filter(entry => entry.id !== id)

    // A delayed queue/transfer heartbeat cannot undo the restart's cancellation or uncertainty.
    if (held?.queueReceipt && held.queueReceipt.ownerKey === ownerKey && held.queueReceipt.status !== 'queued' &&
        (raw.status === 'queued' || raw.status === 'transferred')) {
      next.push(held)

      continue
    }

    if (raw.status === 'transferred' || (raw.status === 'terminal' && raw.outcome === 'completed')) { continue }
    const status: 'queued' | 'terminal' | 'unknown' = raw.status

    const queueReceipt = { status, outcome: typeof raw.outcome === 'string' ? raw.outcome : null,
      ...(typeof raw.reason === 'string' ? { reason: raw.reason } : {}), ownerKey }

    if (isLocalQueueReceiptAcknowledged(id, queueReceipt)) { continue }
    next.push({ id, text: typeof raw.user === 'string' ? raw.user : '', attachments: [],
      queuedAt: held?.queuedAt ?? Date.now(), serverStatus: `local_${status}`, serverControls: [], queueReceipt })
  }

  if (JSON.stringify(next) !== JSON.stringify(getQueuedPrompts(key))) { writeSessionQueue(key, next) }
}

/** Validate the exact owner reference independently of the admission's scalar metadata. */
function blockingUnknownRef(ref: unknown, storedId: string, scope: SessionControlScope): NonNullable<QueuedPromptEntry['observedUnknown']> | undefined {
  if (!ref || typeof ref !== 'object' || !('profile_id' in ref) || !('session_id' in ref) ||
      typeof ref.profile_id !== 'string' || !ref.profile_id || typeof ref.session_id !== 'string' ||
      sessionControlKey(ref.session_id, scope) !== sessionControlKey(storedId, scope)) { return undefined }

  return { ref: { profile_id: ref.profile_id, session_id: ref.session_id }, scope }
}

/** Parse the owner's metadata-only contract; no input_id or user body is read. */
function blockingUnknownReceipt(value: unknown, storedId: string, scope: SessionControlScope): PendingSubmission | undefined {
  if (!value || typeof value !== 'object') { return undefined }
  const raw = value as Record<string, unknown>
  const { admission_id: id, status, execution_generation: generation, controls } = raw
  const observedUnknown = blockingUnknownRef(raw.ref, storedId, scope)

  if (!observedUnknown || typeof id !== 'string' || !id || (status !== 'unknown' && status !== 'terminal') || typeof generation !== 'number' ||
      !Number.isSafeInteger(generation) || generation < 0 || !Array.isArray(controls) ||
      controls.some(control => control !== 'resolve_unknown')) { return undefined }

  return { id, text: '', status: status === 'terminal' ? 'retired' : controls.length ? 'unknown' : 'unavailable', executionGeneration: generation, controls: status === 'unknown' && controls.length ? ['resolve_unknown'] : [],
    controlsOwnerKey: sessionControlOwnerKey(scope), observedUnknown }
}

/** Only the typed pre-admission unknown refusal may publish recovery controls from an error. */
export function recordBlockingUnknownRefusal(error: unknown, storedId: string | null, runtimeId: string, scope: SessionControlScope): void {
  if (!(error instanceof JsonRpcGatewayError) || !error.data || typeof error.data !== 'object') { return }
  const data = error.data as Record<string, unknown>

  if (data.reason === 'SESSION_NOT_OWNED' && data.ownership_reason === 'outcome_unknown') {
    reconcileBlockingUnknown(storedId ?? runtimeId, data.blocking_unknown, scope)
  }
}

/** Visibility/control always rechecks this card's captured owner against the current route. */
export function observedUnknownMatches(storedId: string, entry: QueuedPromptEntry): boolean {
  const observed = entry.observedUnknown

  return !observed || (sessionControlOwnerKey(observed.scope) === sessionControlOwnerKey(captureSessionControlScope(storedId)) &&
    sessionControlKey(observed.ref.session_id, observed.scope) === sessionControlKey(storedId, observed.scope))
}

/** A successful resolve retires only the exact observed barrier, not a newer generation or local input. */
export function retireObservedUnknown(storedId: string, entry: QueuedPromptEntry, result: unknown): boolean {
  if (!entry.observedUnknown || !result || typeof result !== 'object') { return false }
  const receipt = result as Record<string, unknown>
  const scope = entry.observedUnknown.scope
  const ownerKey = sessionControlKey(storedId, scope)
  const before = readPendingSubmissions(ownerKey)
  const held = before[entry.id]
  const ref = blockingUnknownRef(receipt.ref, storedId, scope)

  if (receipt.admission_id !== entry.id || receipt.status !== 'terminal' ||
      receipt.execution_generation !== entry.serverExecutionGeneration ||
      ref?.ref.session_id !== entry.observedUnknown.ref.session_id || ref.ref.profile_id !== entry.observedUnknown.ref.profile_id ||
      held?.observedUnknown?.ref.profile_id !== ref.ref.profile_id || held.executionGeneration !== entry.serverExecutionGeneration) { return false }

  writeEntries(ownerKey, before, { ...before, [entry.id]: { ...held, status: 'retired', controls: [], submittedInput: undefined } })
  writeSessionQueue(journalKey(storedId), getQueuedPrompts(journalKey(storedId)).filter(current =>
    !current.observedUnknown || current.id !== entry.id || current.serverExecutionGeneration !== entry.serverExecutionGeneration ||
    sessionControlOwnerKey(current.observedUnknown.scope) !== sessionControlOwnerKey(scope)))

  return true
}

function revokeUnlistedBlockingUnknown(known: Record<string, PendingSubmission>, receipts: (PendingSubmission | undefined)[] | null, fence?: PendingSnapshotFence): void {
  if (!receipts) { return }

  for (const [id, held] of Object.entries(known)) {
    if (held.observedUnknown && held.status !== 'retired' && !receipts.some(receipt => receipt?.id === id) && !predates(fence, held.seen)) {
      // A listing can be unavailable or show only the earliest barrier. Neither proves terminal.
      known[id] = { ...held, status: 'unavailable', controls: [], ...(fence ? { seen: fence } : {}) }
    }
  }

  const retired = Object.keys(known).filter(id => known[id].status === 'retired')

  for (const id of retired.slice(0, Math.max(0, retired.length - RETIRED_MEMORY))) { delete known[id] }
}

function mergeBlockingUnknownReceipt(held: PendingSubmission | undefined, receipt: PendingSubmission,
  current: QueuedPromptEntry[], fence?: PendingSnapshotFence): PendingSubmission {
  if (held && (predates(fence, held.seen) || (held.executionGeneration ?? -1) > (receipt.executionGeneration ?? -1) ||
      held.observedUnknown?.ref.profile_id !== receipt.observedUnknown?.ref.profile_id ||
      (receipt.status === 'retired' && held.executionGeneration !== receipt.executionGeneration) ||
      (held.status === 'retired' && held.executionGeneration === receipt.executionGeneration))) { return held }

  const own = current.find(entry => entry.id === receipt.id && !entry.observedUnknown && entry.serverStatus)

  const submittedInput = receipt.status === 'retired' ? undefined : held?.submittedInput ?? (own ? {
    text: own.text, displayText: own.displayText, displayKind: own.displayKind, attachments: own.attachments,
    submissionId: own.submissionId, queuedAt: own.queuedAt
  } : undefined)

  return { ...receipt, submittedInput, ...(fence ? { seen: fence } : {}) }
}

function projectBlockingUnknownQueue(current: QueuedPromptEntry[], after: Record<string, PendingSubmission>, scope: SessionControlScope): QueuedPromptEntry[] {
  const next = current.filter(entry => !after[entry.id]?.observedUnknown &&
    (!entry.observedUnknown || sessionControlOwnerKey(entry.observedUnknown.scope) !== sessionControlOwnerKey(scope)))

  for (const receipt of Object.values(after)) {
    if (!receipt.observedUnknown || receipt.status !== 'unknown') { continue }
    next.push({ text: '', attachments: [], queuedAt: Date.now(), ...receipt.submittedInput, id: receipt.id, serverStatus: 'unknown',
      serverControls: receipt.controls, serverExecutionGeneration: receipt.executionGeneration, observedUnknown: receipt.observedUnknown })
  }

  return next
}

/** Observed barriers use the existing journal, in a route-owned slice separate from submitted inputs. */
export function reconcileBlockingUnknown(storedId: string, value: unknown, scope = captureSessionControlScope(storedId), fence?: PendingSnapshotFence, complete = true, ownerInfo?: Pick<SessionRuntimeInfo, 'profile_id' | 'profile_name'>): void {
  if (!Array.isArray(value) || sessionControlOwnerKey(scope) !== sessionControlOwnerKey(captureSessionControlScope(storedId))) { return }
  const key = journalKey(storedId)
  const ownerKey = sessionControlKey(storedId, scope)
  const before = readPendingSubmissions(ownerKey)
  const after = { ...before }

  if (typeof ownerInfo?.profile_name === 'string' && ownerInfo.profile_name !== scope.profile) { return }
  const receipts = value.map(raw => blockingUnknownReceipt(raw, storedId, scope))

  // A malformed listing cannot prove absence, nor acquire controls.
  if (receipts.some(receipt => !receipt || (typeof ownerInfo?.profile_id === 'string' &&
      receipt.observedUnknown?.ref.profile_id !== ownerInfo.profile_id))) { return }

  revokeUnlistedBlockingUnknown(after, complete ? receipts : null, fence)
  const current = getQueuedPrompts(key)

  for (const receipt of receipts) {
    if (!receipt) { continue }
    after[receipt.id] = mergeBlockingUnknownReceipt(before[receipt.id], receipt, current, fence)
  }

  writeEntries(ownerKey, before, after)
  const next = projectBlockingUnknownQueue(current, after, scope)

  if (JSON.stringify(current) !== JSON.stringify(next)) { writeSessionQueue(key, next) }
}

/** The fence of a canonical frame or snapshot, when it carries a usable one. */
export function pendingSnapshotFence(epoch: unknown, sequence: unknown): PendingSnapshotFence | undefined {
  return typeof epoch === 'string' && epoch && typeof sequence === 'number' && Number.isFinite(sequence)
    ? { epoch, sequence }
    : undefined
}

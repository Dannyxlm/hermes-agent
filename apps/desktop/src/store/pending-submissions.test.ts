import { beforeEach, expect, it, vi } from 'vitest'

import { CanonicalDesktopProtocol } from '@/api/canonical-protocol'
import { handleSessionInfoEvent } from '@/app/session/hooks/use-message-stream/gateway-event/session-info'
import type { GatewayEventContext } from '@/app/session/hooks/use-message-stream/gateway-event/types'
import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import { isSteerableEntry } from '@/store/composer-queue'
import type { SessionInfo } from '@/types/hermes'

import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts, migrateQueuedPrompts, removeQueuedPrompt, simulateComposerQueueReloadForTests } from './composer-queue'
import { journaledPendingSessions, readPendingSubmissions, reconcileLocalQueueReceipts, reconcilePendingSubmissions, trackPendingSubmission } from './pending-submissions'
import { $activeGatewayProfile } from './profile'
import { $connection, $sessions } from './session'

beforeEach(() => {
  $sessions.set([])
  $connection.set(null)
  $activeGatewayProfile.set('default')
  window.localStorage.clear()
  $queuedPromptsBySession.set({})
})

it('successful legacy resume control info projects only verified metadata and keeps it separate from submitted pending', () => {
  applyRuntimeInfo({ stored_session_id: 'chat', pending_submissions: [], blocking_unknown: [{ admission_id: 'observed',
    ref: { profile_id: 'default', session_id: 'chat' }, status: 'unknown', execution_generation: 4, controls: ['resolve_unknown'] }] })
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'observed', text: '', attachments: [], serverExecutionGeneration: 4 }])
  expect(readPendingSubmissions('chat').observed).toBeUndefined()
  expect(journaledPendingSessions()).toEqual([])
  reconcilePendingSubmissions('chat', [])
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  applyRuntimeInfo({ stored_session_id: 'chat', pending_submissions: [] })
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [] })
  expect(getQueuedPrompts('chat')).toEqual([])
})

it.each([
  undefined, null, [], [{}],
  [{ admission_id: 'x', status: 'unknown', execution_generation: 4, controls: ['resolve_unknown'] }],
  ...[
    { ref: { profile_id: 'foreign', session_id: 'chat' } },
    { ref: { profile_id: 'default', session_id: 'wrong-chat' } },
    { execution_generation: undefined }, { execution_generation: '4' }, { execution_generation: -1 },
    { execution_generation: 1.5 }, { execution_generation: NaN },
    { controls: [] }, { controls: ['interrupt'] }, { controls: ['resolve_unknown', 'cancel'] },
    { admission_id: '' }, { status: 'started' }
  ].map(patch => [{ admission_id: 'x', ref: { profile_id: 'default', session_id: 'chat' }, status: 'unknown',
    execution_generation: 4, controls: ['resolve_unknown'], ...patch }])
])('unverified blocking metadata never acquires a card or blocks the quiet queue: %j', value => {
  enqueueQueuedPrompt('chat', { id: 'quiet', text: 'quiet local', attachments: [] })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: value })
  expect(getQueuedPrompts('chat').map(entry => [entry.id, entry.serverStatus])).toEqual([['quiet', undefined]])
})

it('older fences/generations and foreign sources cannot retire or replace the correct observed evidence', () => {
  const value = (generation: number) => [{ admission_id: 'observed', ref: { profile_id: 'default', session_id: 'chat' },
    status: 'unknown', execution_generation: generation, controls: ['resolve_unknown'] }]

  const info = (sequence: number, blocking_unknown: unknown) => ({ stored_session_id: 'chat', blocking_unknown, replay_epoch: 'epoch', last_sequence: sequence })
  applyRuntimeInfo(info(10, value(9)))
  applyRuntimeInfo(info(7, []))
  applyRuntimeInfo(info(11, value(8)))
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'observed', serverExecutionGeneration: 9 }])
  applyRuntimeInfo(info(12, []), { controlScope: { connectionId: 'foreign', profile: 'default' } })
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  applyRuntimeInfo({ stored_session_id: 'other', blocking_unknown: [] })
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  applyRuntimeInfo(info(13, []))
  expect(getQueuedPrompts('chat')).toEqual([])
  applyRuntimeInfo(info(10, value(9)))
  expect(getQueuedPrompts('chat')).toEqual([])
})

it('a fresh owner receipt with no served control revokes the observed Discard without making a replayable input', () => {
  const info = (controls: string[]) => ({ stored_session_id: 'chat', blocking_unknown: [{ admission_id: 'observed',
    ref: { profile_id: 'default', session_id: 'chat' }, status: 'unknown', execution_generation: 4, controls }] })

  applyRuntimeInfo(info(['resolve_unknown']))
  applyRuntimeInfo(info([]))
  expect(getQueuedPrompts('chat')).toEqual([])
  applyRuntimeInfo(info(['resolve_unknown']))
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'observed', serverControls: ['resolve_unknown'] }])
})

it('observed metadata never reads another viewer input text or submission identity', () => {
  const row = { admission_id: 'observed', ref: { profile_id: 'default', session_id: 'chat' },
    status: 'unknown', execution_generation: 4, controls: ['resolve_unknown'] }

  Object.defineProperties(row, {
    user: { get: () => { throw new Error('private input read') } },
    input_id: { get: () => { throw new Error('submission identity read') } }
  })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [row] })
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'observed', text: '', serverControls: ['resolve_unknown'] }])
})

it('projects resumed runtime pending receipts under durable identity without making them sendable', () => {
  const info = {
    stored_session_id: 'durable',
    pending_submissions: [
      { admission_id: 'remote', status: 'queued', user: 'queued exact Ω' },
      { admission_id: 'uncertain', status: 'unknown', user: 'unknown exact Ω' }
    ]
  }

  applyRuntimeInfo(info)
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('durable').map(({ id, text, serverStatus }) => ({ id, text, serverStatus })))
    .toEqual(info.pending_submissions.map(({ admission_id, user, status }) => ({ id: admission_id, text: user, serverStatus: status })))
  expect(getQueuedPrompts('durable').every(entry => !isSteerableEntry(entry))).toBe(true)
  expect(getQueuedPrompts('runtime')).toEqual([])
})

it('carries the queued-row controls the owner lists onto the card, and none when it lists none (RT-35)', () => {
  reconcilePendingSubmissions('chat', [
    { admission_id: 'served', status: 'queued', user: 'edit me', controls: ['steer', 'replace'] },
    { admission_id: 'older', status: 'queued', user: 'older owner' }
  ])

  expect(getQueuedPrompts('chat').map(({ id, serverControls }) => ({ id, serverControls }))).toEqual([
    { id: 'served', serverControls: ['steer', 'replace'] },
    { id: 'older', serverControls: undefined }
  ])
  expect(readPendingSubmissions('chat').served.controls).toEqual(['steer', 'replace'])
})

it('a steer in transfer hides its card without retiring it, so a return to the queue repaints it in place (RT-35)', () => {
  const later = { admission_id: 'later', status: 'queued', user: 'queued after it' }
  reconcilePendingSubmissions('chat', [{ admission_id: 'moving', status: 'queued', user: 'steer me' }, later])
  // Handed to the running turn: listed `started` and `steering` until that turn settles it.
  reconcilePendingSubmissions('chat', [
    { admission_id: 'moving', status: 'started', steering: true, user: 'steer me' },
    later
  ])
  expect(getQueuedPrompts('chat').map(({ id }) => id)).toEqual(['later'])

  // The text never reached the turn: the owner puts the same admission back in its FIFO place, ahead
  // of the admission queued after it, and the card is shown where the gateway will run it.
  reconcilePendingSubmissions('chat', [{ admission_id: 'moving', status: 'queued', user: 'steer me' }, later])
  expect(getQueuedPrompts('chat').map(({ id, serverStatus }) => ({ id, serverStatus }))).toEqual([
    { id: 'moving', serverStatus: 'queued' },
    { id: 'later', serverStatus: 'queued' }
  ])
  reconcilePendingSubmissions('chat', [later])

  // An ordinary claim still only moves forward: a replayed `queued` never repaints a started row.
  reconcilePendingSubmissions('chat', [{ admission_id: 'moving', status: 'started', user: 'steer me' }])
  reconcilePendingSubmissions('chat', [{ admission_id: 'moving', status: 'queued', user: 'steer me' }])
  expect(getQueuedPrompts('chat')).toEqual([])
})

it('reconciles server queue by identity without replaying or duplicating local entries', () => {
  enqueueQueuedPrompt('chat', { id: 'one', text: 'same', attachments: [] })
  enqueueQueuedPrompt('chat', { id: 'local', text: 'same', attachments: [] })
  const snapshot = [{ admission_id: 'one', status: 'queued', user: 'same' }]
  reconcilePendingSubmissions('chat', snapshot)
  reconcilePendingSubmissions('chat', snapshot)
  expect(getQueuedPrompts('chat').map(entry => entry.id)).toEqual(['one', 'local'])
  expect(getQueuedPrompts('chat')[0]?.serverStatus).toBe('queued')
  reconcilePendingSubmissions('chat', [{ ...snapshot[0], status: 'started' }])
  expect(getQueuedPrompts('chat').map(entry => entry.id)).toEqual(['local'])
  reconcilePendingSubmissions('chat', [{ admission_id: 'unknown', status: 'unknown', user: 'interrupted' }])
  expect(getQueuedPrompts('chat').find(entry => entry.id === 'unknown')?.serverStatus).toBe('unknown')
  reconcilePendingSubmissions('chat', [])
  expect(getQueuedPrompts('chat').map(entry => entry.id)).toEqual(['local'])
})

it('recovers remote pending text into the queue and journal without a local submission', () => {
  const snapshot = [
    { admission_id: 'remote-queued', status: 'queued', user: 'queued elsewhere' },
    { admission_id: 'remote-unknown', status: 'unknown', user: 'interrupted elsewhere' }
  ]

  reconcilePendingSubmissions('chat', snapshot)
  reconcilePendingSubmissions('chat', snapshot)
  expect(getQueuedPrompts('chat').map(({ id, text, serverStatus }) => ({ id, text, serverStatus })))
    .toEqual(snapshot.map(({ admission_id, user, status }) => ({ id: admission_id, text: user, serverStatus: status })))
  const journal = readPendingSubmissions('chat')

  for (const receipt of snapshot) {
    expect(journal[receipt.admission_id].text).toBe(receipt.user)
  }
})

it('a stale queued projection cannot replace unknown execution identity or Discard controls', () => {
  reconcilePendingSubmissions('chat', [{ admission_id: 'forwarded', input_id: 'input', status: 'unknown', user: 'lost', execution_generation: 8, controls: ['resolve_unknown'] }])
  reconcilePendingSubmissions('chat', [{ admission_id: 'forwarded', input_id: 'input', status: 'queued', user: 'lost', execution_generation: 3, controls: ['cancel'] }])
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'forwarded', serverStatus: 'unknown', serverExecutionGeneration: 8, serverControls: ['resolve_unknown'] }])
})

it('retains actual legacy forwarded Stop across a stale queued frame and renderer reload', () => {
  reconcilePendingSubmissions('chat', [{ admission_id: 'legacy', status: 'started', user: 'running', execution_generation: 9, controls: ['interrupt'] }])
  simulateComposerQueueReloadForTests()
  reconcilePendingSubmissions('chat', [{ admission_id: 'legacy', status: 'queued', user: 'old', execution_generation: 1, controls: ['cancel'] }])
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'legacy', serverStatus: 'started', serverExecutionGeneration: 9, serverControls: ['interrupt'] }])
})

it.each(['profile', 'connection'])('does not borrow controls from a same-id foreign %s receipt', dimension => {
  reconcilePendingSubmissions('chat', [{ admission_id: 'foreign', status: 'unknown', user: 'lost', execution_generation: 8, controls: ['resolve_unknown'] }])

  if (dimension === 'profile') { $activeGatewayProfile.set('foreign') }
  else { $connection.set({ mode: 'remote', connectionId: 'other' } as never) }

  reconcilePendingSubmissions('chat', [{ admission_id: 'foreign', status: 'queued', user: 'old', execution_generation: 1, controls: ['cancel'] }])
  expect(getQueuedPrompts('chat')).toMatchObject([{ serverStatus: 'unknown', serverControls: [] }])
})

it('a stale queued frame cannot invent canonical Stop permission or borrow a foreign owner control', () => {
  reconcilePendingSubmissions('chat', [{ admission_id: 'canonical', status: 'started', user: 'no Stop', execution_generation: 8 }])
  reconcilePendingSubmissions('chat', [{ admission_id: 'canonical', status: 'queued', user: 'old', controls: ['interrupt'], execution_generation: 1 }])
  expect(getQueuedPrompts('chat')).toEqual([])
  reconcilePendingSubmissions('chat', [{ admission_id: 'foreign', status: 'unknown', user: 'lost', execution_generation: 8, controls: ['resolve_unknown'] }])
  $activeGatewayProfile.set('foreign')
  reconcilePendingSubmissions('chat', [{ admission_id: 'foreign', status: 'queued', user: 'old', execution_generation: 1, controls: ['cancel'] }])
  expect(getQueuedPrompts('chat').every(entry => !entry.serverControls?.length)).toBe(true)
})

it.each([
  ['unknown', 'profile'], ['terminal', 'profile'], ['unknown', 'connection'], ['terminal', 'connection']
])('acknowledges a local %s receipt across info, resume and renderer reload, but not new evidence or another %s', (status, dimension) => {
  const receipt = { input_id: 'local-input', session_id: 'chat', status, user: 'input', outcome: status === 'terminal' ? 'cancelled' : null, reason: 'restart' }
  const info = { stored_session_id: 'chat', queue_receipts: [receipt] }
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  expect(removeQueuedPrompt('chat', 'local-queue:local-input')).toBe(true)
  reconcileLocalQueueReceipts('chat', [receipt])
  expect(getQueuedPrompts('chat')).toEqual([])
  simulateComposerQueueReloadForTests()
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toEqual([])
  reconcileLocalQueueReceipts('chat', [{ ...receipt, reason: 'new evidence' }])
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  expect(removeQueuedPrompt('chat', 'local-queue:local-input')).toBe(true)

  if (dimension === 'profile') { $activeGatewayProfile.set('foreign') }
  else { $connection.set({ mode: 'remote', connectionId: 'foreign' } as never) }

  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toHaveLength(1)
})

it('new terminal evidence resurfaces after acknowledging unknown without replay or an RPC', () => {
  const receipt = { input_id: 'progress', session_id: 'chat', user: 'input', outcome: null }
  reconcileLocalQueueReceipts('chat', [{ ...receipt, status: 'unknown' }])
  expect(removeQueuedPrompt('chat', 'local-queue:progress')).toBe(true)
  reconcileLocalQueueReceipts('chat', [{ ...receipt, status: 'terminal', outcome: 'cancelled' }])
  expect(getQueuedPrompts('chat')).toMatchObject([{ queueReceipt: { status: 'terminal', outcome: 'cancelled' }, serverControls: [] }])
  expect(getQueuedPrompts('chat').every(entry => !isSteerableEntry(entry))).toBe(true)
})

it('a late transferred frame cannot erase unknown, while an actual completion may settle it', () => {
  const receipt = { input_id: 'local-input', session_id: 'chat', user: 'input', outcome: null }
  reconcileLocalQueueReceipts('chat', [{ ...receipt, status: 'unknown', reason: 'backend_restart_after_dispatch' }])
  reconcileLocalQueueReceipts('chat', [{ ...receipt, status: 'transferred' }])
  expect(getQueuedPrompts('chat')).toMatchObject([{ queueReceipt: { status: 'unknown' } }])
  reconcileLocalQueueReceipts('chat', [{ ...receipt, status: 'terminal', outcome: 'completed' }])
  expect(getQueuedPrompts('chat')).toEqual([])
})

it('maps optimistic input identity to the admission identity before local drain can replay it', () => {
  enqueueQueuedPrompt('mapped', { id: 'input-id', text: 'same', attachments: [] })
  const receipt = { admission_id: 'admission-id', input_id: 'input-id', status: 'queued', user: 'same' }
  reconcilePendingSubmissions('mapped', [receipt])
  expect(getQueuedPrompts('mapped')).toMatchObject([{ id: 'admission-id', serverStatus: 'queued', text: 'same' }])
  expect(getQueuedPrompts('mapped')).toHaveLength(1)
  reconcilePendingSubmissions('mapped', [{ ...receipt, status: 'started' }])
  expect(getQueuedPrompts('mapped')).toEqual([])
})

it('persists identified direct submissions independently of the automatic local queue', () => {
  trackPendingSubmission('chat', { id: 'direct', text: 'hello' })
  expect(readPendingSubmissions('chat').direct).toMatchObject({ id: 'direct', text: 'hello' })
  expect(getQueuedPrompts('chat')).toEqual([])
})

it('never lets a stale queue snapshot repaint an admission already seen started or retired', () => {
  const queued = [{ admission_id: 'a', status: 'queued', user: 'later' }]
  reconcilePendingSubmissions('mono', queued)
  reconcilePendingSubmissions('mono', [{ ...queued[0], status: 'started' }])
  reconcilePendingSubmissions('mono', queued)
  expect(getQueuedPrompts('mono')).toEqual([])

  reconcilePendingSubmissions('mono', [])
  reconcilePendingSubmissions('mono', queued)
  expect(getQueuedPrompts('mono')).toEqual([])

  // A started turn the owner lost across a restart legitimately moves on to `unknown`.
  reconcilePendingSubmissions('mono', [{ admission_id: 'b', status: 'started', user: 'lost' }])
  reconcilePendingSubmissions('mono', [{ admission_id: 'b', status: 'unknown', user: 'lost' }])
  expect(getQueuedPrompts('mono').map(entry => [entry.id, entry.serverStatus])).toEqual([['b', 'unknown']])
})

it('a delayed older empty snapshot never retires a receipt a newer frame still lists; a current one does', () => {
  const protocol = new CanonicalDesktopProtocol()
  const queued = { admission_id: 'q', input_id: 'in-q', status: 'queued', text: 'still waiting', sequence: 1 }

  // The owner's resume answer, through the production snapshot projection.
  const snapshot = (lastSequence: number, pending: unknown[]) => applyRuntimeInfo(protocol.result('session.resume', { session_id: 'fenced' },
    { session_id: 'fenced', stored_session_id: 'fenced', revision: 1, execution_generation: 1, replay_epoch: 'e1', last_sequence: lastSequence, pending, info: {} }).info,
  { foreground: false })

  // The live pending fanout at seq 10, through the canonical normalizer and the session.info handler.
  const live = { type: 'session.info', session_id: 'fenced', seq: 10, replay_epoch: 'e1', profile: 'default',
    payload: { stored_session_id: 'fenced', pending: [queued], revision: 1 } as Record<string, unknown> }

  protocol.event(live)
  handleSessionInfoEvent({
    deps: { activeGatewayProfile: 'default', activeSessionIdRef: { current: null }, hydrateFromStoredSession: vi.fn(),
      lastCwdInfoSessionRef: { current: null }, queryClient: { invalidateQueries: vi.fn() }, refreshHermesConfig: vi.fn(),
      scheduleSessionsRefresh: vi.fn(), sessionInterrupted: () => false, sessionStateByRuntimeIdRef: { current: new Map() },
      updateSessionState: vi.fn(state => state), upsertToolCall: vi.fn() },
    event: live, explicitSid: 'fenced', fromActiveSource: () => true, isActiveEvent: false, occurredAt: Date.now() / 1000,
    payload: live.payload, scheduleConfigRefresh: vi.fn(), sessionId: 'fenced'
  } as unknown as GatewayEventContext)
  expect(getQueuedPrompts('fenced').map(entry => [entry.id, entry.serverStatus])).toEqual([['q', 'queued']])

  // A resume answer read at seq 7 arrives late and lists nothing: it predates the queued frame.
  snapshot(7, [])
  expect(readPendingSubmissions('fenced').q?.status).toBe('queued')
  expect(getQueuedPrompts('fenced').map(entry => [entry.id, entry.serverStatus])).toEqual([['q', 'queued']])

  // A current snapshot (seq 12) without it is the authority's word: the receipt retires.
  snapshot(12, [])
  expect(readPendingSubmissions('fenced').q?.status).toBe('retired')
  expect(getQueuedPrompts('fenced')).toEqual([])
})

// A replayed or delayed snapshot that still lists an `unknown` admission as `started` is stale for
// its status, not for its presence: the Discard card the owner's restart produced must stay.
it('a stale started snapshot keeps an existing unknown card and its Discard control', () => {
  reconcilePendingSubmissions('lost', [{ admission_id: 'b', status: 'started', user: 'lost' }])
  reconcilePendingSubmissions('lost', [{ admission_id: 'b', status: 'unknown', user: 'lost' }])
  reconcilePendingSubmissions('lost', [{ admission_id: 'b', status: 'started', user: 'lost' }])
  expect(getQueuedPrompts('lost').map(entry => [entry.id, entry.serverStatus])).toEqual([['b', 'unknown']])
  expect(readPendingSubmissions('lost').b?.status).toBe('unknown')

  // Its real absence from a current snapshot still retires it.
  reconcilePendingSubmissions('lost', [])
  expect(getQueuedPrompts('lost')).toEqual([])
})

// After compression the panel reads the lineage root while the runtime reports the tip. A tip-keyed
// projection only reached the panel through rotation migrations, which stacked copies there that no
// later snapshot could retire: duplicate, undeletable, back after a delete (#138934).
it('projects a compressed tip snapshot onto the lineage key the panel reads', () => {
  $sessions.set([{ id: 'tip', _lineage_root_id: 'root' } as SessionInfo])

  try {
    const listed = [{ admission_id: 'x', status: 'queued', user: 'later' }]
    reconcilePendingSubmissions('tip', listed, { epoch: 'e', sequence: 1 })
    migrateQueuedPrompts('tip', 'root')
    reconcilePendingSubmissions('tip', listed, { epoch: 'e', sequence: 2 })
    migrateQueuedPrompts('tip', 'root')
    expect(getQueuedPrompts('root').map(entry => [entry.id, entry.serverStatus])).toEqual([['x', 'queued']])

    // Withdrawn at the gateway: the next snapshot no longer lists it.
    reconcilePendingSubmissions('tip', [], { epoch: 'e', sequence: 3 })
    expect(getQueuedPrompts('root')).toEqual([])

    // The same earlier status mirrored again does not bring it back.
    reconcilePendingSubmissions('tip', listed, { epoch: 'e', sequence: 2 })
    migrateQueuedPrompts('tip', 'root')
    expect(getQueuedPrompts('root')).toEqual([])
  } finally {
    $sessions.set([])
  }
})

// The tip->root row can load after a snapshot was already journaled under the tip. The fence that
// snapshot recorded must follow the card to the root, or a delayed older empty one hides it.
it('late lineage discovery keeps the ordering fence: an older empty snapshot leaves the card', () => {
  try {
    reconcilePendingSubmissions('tip', [{ admission_id: 'x', status: 'queued', user: 'waiting' }], { epoch: 'e', sequence: 10 })
    expect(getQueuedPrompts('tip').map(entry => entry.id)).toEqual(['x'])

    $sessions.set([{ id: 'tip', _lineage_root_id: 'root' } as SessionInfo])
    migrateQueuedPrompts('tip', 'root')

    reconcilePendingSubmissions('tip', [], { epoch: 'e', sequence: 7 })
    expect(getQueuedPrompts('root').map(entry => [entry.id, entry.serverStatus])).toEqual([['x', 'queued']])
    expect(readPendingSubmissions('root').x?.status).toBe('queued')

    // A current empty snapshot still retires it.
    reconcilePendingSubmissions('tip', [], { epoch: 'e', sequence: 12 })
    expect(getQueuedPrompts('root')).toEqual([])
  } finally {
    $sessions.set([])
  }
})

// Submit tracks under the stored id it targets, which can be the tip. Its stronger status and the
// display text only it knows must still meet the root-projected snapshots.
it('a tip-keyed tracked receipt keeps its status, text and Discard across root projection', () => {
  try {
    trackPendingSubmission('tip', { id: 'u', text: 'full prompt', displayText: '/cmd', status: 'unknown' })
    $sessions.set([{ id: 'tip', _lineage_root_id: 'root' } as SessionInfo])

    // A replayed `started` is stale for an admission already answered `unknown`.
    reconcilePendingSubmissions('tip', [{ admission_id: 'u', status: 'started', user: 'full prompt' }], { epoch: 'e', sequence: 1 })
    reconcilePendingSubmissions('tip', [{ admission_id: 'u', status: 'started', user: 'full prompt' }], { epoch: 'e', sequence: 2 })

    expect(getQueuedPrompts('root').map(({ id, text, displayText, serverStatus }) => ({ id, text, displayText, serverStatus })))
      .toEqual([{ id: 'u', text: 'full prompt', displayText: '/cmd', serverStatus: 'unknown' }])
    expect(readPendingSubmissions('root').u).toMatchObject({ status: 'unknown', displayText: '/cmd' })
    expect(readPendingSubmissions('tip')).toEqual({})

    // Tracking once the lineage is known lands where reconciliation reads.
    trackPendingSubmission('tip', { id: 'v', text: 'next', displayText: '/next' })
    expect(readPendingSubmissions('root').v).toMatchObject({ displayText: '/next' })
  } finally {
    $sessions.set([])
  }
})

// Every send tracks its admission before the request and again after the reply, on the renderer
// thread. That must cost the one admission's record, not a parse of every saved session's journal.
it('tracking a submission reads its own record without enumerating other sessions', () => {
  for (let index = 0; index < 20; index += 1) {
    trackPendingSubmission(`other-${index}`, { id: `a-${index}`, text: 'elsewhere', status: 'queued' })
  }

  window.localStorage.setItem('hermes.desktop.pendingSubmissions.v1', JSON.stringify({ solo: { old: { id: 'old', text: 'legacy', displayText: '/old' } } }))
  const enumerate = vi.spyOn(window.localStorage, 'key')

  try {
    trackPendingSubmission('solo', { id: 'mine', text: 'hi', displayText: '/hi' })
    trackPendingSubmission('solo', { id: 'mine', text: 'hi', status: 'queued' })
    // A legacy receipt still merges into its tracked successor.
    trackPendingSubmission('solo', { id: 'old', text: '', status: 'unknown' })
    expect(enumerate).not.toHaveBeenCalled()

    // A newly known tip->root lineage adopts the tip's journal once; later sends read directly.
    trackPendingSubmission('late-tip', { id: 'early', text: 'early', displayText: '/early', status: 'unknown' })
    $sessions.set([{ id: 'late-tip', _lineage_root_id: 'late-root' } as SessionInfo])
    trackPendingSubmission('late-tip', { id: 'next', text: 'next' })
    expect(readPendingSubmissions('late-root').early).toMatchObject({ status: 'unknown', displayText: '/early' })
    enumerate.mockClear()
    trackPendingSubmission('late-tip', { id: 'next', text: 'next', status: 'queued' })
    expect(enumerate).not.toHaveBeenCalled()
  } finally {
    enumerate.mockRestore()
    $sessions.set([])
  }

  expect(readPendingSubmissions('solo')).toMatchObject({
    mine: { text: 'hi', displayText: '/hi', status: 'queued' },
    old: { text: 'legacy', displayText: '/old', status: 'unknown' }
  })
  expect(readPendingSubmissions('late-root').next).toMatchObject({ status: 'queued' })
})

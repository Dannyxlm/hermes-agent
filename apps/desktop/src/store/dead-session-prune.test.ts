import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/api/client'
import type * as HermesModule from '@/hermes'
import type { ProfileInfo, SessionInfo } from '@/types/hermes'

const getSessionMock = vi.fn<(id: string, profile?: null | string) => Promise<SessionInfo>>()

vi.mock('@/hermes', async importOriginal => {
  const actual = await importOriginal<typeof HermesModule>()

  return {
    ...actual,
    getSession: (id: string, profile?: null | string) => getSessionMock(id, profile)
  }
})

import { clearSessionDraft, stashSessionDraft, takeSessionDraft } from '@/store/composer'
import { interruptedSendScopes } from '@/store/composer-outbox'
import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import { $connectionsRegistry } from '@/store/connection-registry-state'
import { $pinnedSessionIds } from '@/store/layout'
import { $activeGatewayProfile, $profiles } from '@/store/profile'
import { $sessions, $unlistedSessionOwnerRows, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { $sessionTiles } from '@/store/session-states'

import { __runDeadSessionPrunePass, resetDeadSessionPrune, watchDeadSessionPrune } from './dead-session-prune'

const row = (id: string, extra: Partial<SessionInfo> = {}): SessionInfo =>
  ({ id, message_count: 1, source: 'cli', started_at: 0, title: id, ...extra }) as SessionInfo

const profile = (name: string): ProfileInfo =>
  ({
    has_env: false,
    is_default: name === 'default',
    model: null,
    name,
    path: name,
    provider: null,
    skill_count: 0
  }) as ProfileInfo

const notFound = () => new Error('404: {"detail":"Session not found"}')

beforeAll(() => {
  ;(globalThis as { window?: unknown }).window ??= {}
  ;(window as unknown as { hermesDesktop: unknown }).hermesDesktop = {}
})

beforeEach(() => {
  window.localStorage.clear()
  $connectionsRegistry.set(null)
  $unlistedSessionOwnerRows.set([])
  $sessionTiles.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
  setApiRequestConnection(null)
  $sessions.set([])
  $pinnedSessionIds.set([])
  $queuedPromptsBySession.set({})
  $profiles.set([profile('default'), profile('work')])
  $activeGatewayProfile.set('default')
  getSessionMock.mockReset()
  resetDeadSessionPrune()
})

afterEach(() => {
  $connectionsRegistry.set(null)
  $unlistedSessionOwnerRows.set([])
  $sessionTiles.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
  clearSessionDraft('dead-draft-session')
  clearSessionDraft('worker')
  clearSessionDraft('dead')
  clearSessionDraft('__new__')
  $sessions.set([])
  $pinnedSessionIds.set([])
  $queuedPromptsBySession.set({})
})

describe('__runDeadSessionPrunePass', () => {
  it('preserves restart references for a readable non-ambient worker without an owner hint, but still prunes an exactly owned dead id', async () => {
    const actual = await vi.importActual<typeof HermesModule>('@/hermes')
    getSessionMock.mockImplementation(actual.getSession)
    $connectionsRegistry.set({ version: 1, primary: 'local', secureTokenStorage: true,
      connections: ['local', 'rig'].map(id => ({ id, kind: id === 'local' ? 'local' : 'ssh', label: id, tokenSet: false, tokenPreview: null })) })
    setApiRequestConnection('local')

    const api = vi.fn(async request => {
      const id = new URL(request.path, 'http://test').pathname.split('/').pop()

      if (id === 'worker' && request.connectionId === 'rig' && request.profile === 'work') {
        return row('worker', { profile: 'work', thread_parent_id: 'lead' })
      }

      throw notFound()
    })

    vi.stubGlobal('hermesDesktop', { api })
    // Only the human lists after restart. Pins/drafts survive, but the worker's
    // off-list row, tile and transient owner evidence have not been recovered.
    $sessions.set([row('human', { connection_id: 'rig', profile: 'work' })])
    $pinnedSessionIds.set(['worker', 'human', 'dead'])
    stashSessionDraft('worker', 'unsent worker words', [])
    stashSessionDraft('dead', 'gone words', [])
    enqueueQueuedPrompt('worker', { text: 'next worker prompt', attachments: [] })
    enqueueQueuedPrompt('dead', { text: 'gone prompt', attachments: [] })

    for (const scope of ['worker', 'dead']) {
      window.localStorage.setItem(`hermes:composer-outbox:v1:${JSON.stringify([scope, 'send'])}`, JSON.stringify({
        scope, at: Date.now(), retry: { id: 'send', text: 'interrupted', attachmentIds: [] }
      }))
    }

    setSessionOwnerHint('dead', { connectionId: 'rig', profile: 'work' })
    expect($unlistedSessionOwnerRows.get()).toEqual([])
    expect(await actual.getSession('worker', { connectionId: 'rig', profile: 'work' })).toMatchObject({ thread_parent_id: 'lead' })
    api.mockClear()

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual(['worker', 'human'])
    expect(takeSessionDraft('worker').text).toBe('unsent worker words')
    expect(getQueuedPrompts('worker')).toHaveLength(1)
    expect(interruptedSendScopes()).toEqual(['worker'])
    expect(takeSessionDraft('dead').text).toBe('')
    expect(getQueuedPrompts('dead')).toEqual([])
    expect(api.mock.calls.some(([request]) => request.path.includes('/human'))).toBe(false)
    expect(api.mock.calls.filter(([request]) => request.path.includes('/dead')).every(([request]) => request.connectionId === 'rig')).toBe(true)
  })

  it('treats parked Family rows and their roots as covered even when their source is offline', async () => {
    $unlistedSessionOwnerRows.set([row('worker-tip', { _lineage_root_id: 'worker-root', connection_id: 'rig', thread_parent_id: 'lead' })])
    $pinnedSessionIds.set(['worker-tip', 'worker-root'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual(['worker-tip', 'worker-root'])
    expect(getSessionMock).not.toHaveBeenCalled()
  })

  it('rechecks parked Family rows before applying an in-flight dead verdict', async () => {
    $pinnedSessionIds.set(['worker'])
    let rejectProbe!: (error: Error) => void
    getSessionMock.mockReturnValueOnce(new Promise((_resolve, reject) => { rejectProbe = reject }))
    getSessionMock.mockRejectedValue(notFound())
    const pass = __runDeadSessionPrunePass()
    $unlistedSessionOwnerRows.set([row('worker', { connection_id: 'rig', thread_parent_id: 'lead' })])
    rejectProbe(notFound())
    await pass

    expect($pinnedSessionIds.get()).toEqual(['worker'])
  })

  it('still prunes a deleted unowned id when the loaded registry has only the ambient source', async () => {
    $connectionsRegistry.set({ version: 1, primary: 'local', secureTokenStorage: true,
      connections: [{ id: 'local', kind: 'local', label: 'This device', tokenSet: false, tokenPreview: null }] })
    $pinnedSessionIds.set(['dead'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual([])
  })

  it('unpins a pin whose session 404s on every profile', async () => {
    $sessions.set([row('unrelated')])
    $pinnedSessionIds.set(['dead-pin'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual([])
    // Active-scope probe first, then each other profile.
    expect(getSessionMock).toHaveBeenCalledWith('dead-pin', undefined)
    expect(getSessionMock).toHaveBeenCalledWith('dead-pin', 'work')
  })

  it('keeps a pin whose session lives on another profile, and caches it as alive', async () => {
    $sessions.set([row('unrelated')])
    $pinnedSessionIds.set(['other-pin'])
    getSessionMock.mockImplementation(async (id, profileName) => {
      if (profileName === 'work') {
        return row(id, { profile: 'work' })
      }

      throw notFound()
    })

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual(['other-pin'])

    // Alive verdict is cached — a second pass must not re-probe.
    getSessionMock.mockClear()
    await __runDeadSessionPrunePass()
    expect(getSessionMock).not.toHaveBeenCalled()
  })

  it('defers on non-404 probe failures instead of dropping, and retries later', async () => {
    $sessions.set([row('unrelated')])
    $pinnedSessionIds.set(['flaky-pin'])
    getSessionMock.mockRejectedValue(new Error('gateway restarting'))

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual(['flaky-pin'])

    // Not cached — the next pass probes again, and a definitive 404 prunes.
    getSessionMock.mockRejectedValue(notFound())
    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual([])
  })

  it('discards drafts for dead sessions but never the new-chat draft', async () => {
    $sessions.set([row('unrelated')])
    stashSessionDraft('dead-draft-session', 'stale text', [])
    stashSessionDraft('__new__', 'brand new', [])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect(takeSessionDraft('dead-draft-session').text).toBe('')
    expect(takeSessionDraft('__new__').text).toBe('brand new')
  })

  it('drops queued prompts for dead sessions and keeps live ones', async () => {
    $sessions.set([row('unrelated')])
    enqueueQueuedPrompt('dead-queue-session', { text: 'go', attachments: [] })
    enqueueQueuedPrompt('live-queue-session', { text: 'stay', attachments: [] })
    getSessionMock.mockImplementation(async (id, profileName) => {
      if (id === 'live-queue-session') {
        return row(id, { profile: profileName ?? 'default' })
      }

      throw notFound()
    })

    await __runDeadSessionPrunePass()

    expect(getQueuedPrompts('dead-queue-session')).toEqual([])
    expect(getQueuedPrompts('live-queue-session')).toHaveLength(1)
  })

  it('skips ids already covered by loaded sidebar rows', async () => {
    $sessions.set([row('covered', { _lineage_root_id: 'root-covered' })])
    $pinnedSessionIds.set(['covered', 'root-covered', 'rowless-pin'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect(getSessionMock).not.toHaveBeenCalledWith('covered', expect.anything())
    expect(getSessionMock).not.toHaveBeenCalledWith('root-covered', expect.anything())
    expect(getSessionMock).toHaveBeenCalledWith('rowless-pin', undefined)
    // Covered pins are alive (their rows exist) — only the rowless pin dies.
    expect($pinnedSessionIds.get()).toEqual(['covered', 'root-covered'])
  })

  it('defers when the profile list is not loaded yet', async () => {
    $sessions.set([row('unrelated')])
    $profiles.set([])
    $pinnedSessionIds.set(['mystery-pin'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    // Can't rule out another profile we don't know about — keep the pin.
    expect($pinnedSessionIds.get()).toEqual(['mystery-pin'])
  })

  it('prunes on a genuinely empty backend (all sessions purged)', async () => {
    // An empty list is a real answer here — the wipe path is handled by the
    // gateway-switch reset cancelling the wipe-scheduled sweep, not by the
    // sweep refusing to run. A backend with zero sessions has no rows to
    // cover stored ids, so probes decide.
    $sessions.set([])
    $pinnedSessionIds.set(['orphan-pin'])
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual([])
    expect(getSessionMock).toHaveBeenCalledWith('orphan-pin', undefined)
  })

  it('re-probes an alive id once its verdict TTL expires', async () => {
    vi.useFakeTimers()

    try {
      $sessions.set([row('unrelated')])
      $pinnedSessionIds.set(['ttl-pin'])
      getSessionMock.mockImplementation(async (id, profileName) => {
        if (profileName === 'work') {
          return row(id, { profile: 'work' })
        }

        throw notFound()
      })

      // Alive on another profile — cached, not pruned, not re-probed.
      await __runDeadSessionPrunePass()
      expect($pinnedSessionIds.get()).toEqual(['ttl-pin'])
      getSessionMock.mockClear()
      await __runDeadSessionPrunePass()
      expect(getSessionMock).not.toHaveBeenCalled()

      // Session deleted mid-session; after the TTL the next pass re-probes
      // and a definitive all-profile 404 prunes.
      vi.setSystemTime(Date.now() + 10 * 60_000 + 1_000)
      getSessionMock.mockRejectedValue(notFound())
      await __runDeadSessionPrunePass()

      expect($pinnedSessionIds.get()).toEqual([])
    } finally {
      vi.useRealTimers()
    }
  })

  it('discards in-flight verdicts when a gateway reset happens mid-sweep', async () => {
    $sessions.set([row('unrelated')])
    $pinnedSessionIds.set(['race-pin'])
    let resolveProbe!: (value: SessionInfo) => void
    getSessionMock.mockImplementation(
      () =>
        new Promise(resolve => {
          resolveProbe = resolve
        })
    )

    const pass = __runDeadSessionPrunePass()
    // Gateway switch while the probe is in flight.
    resetDeadSessionPrune()
    resolveProbe(row('race-pin', { profile: 'default' }))
    await pass

    // The pre-reset verdict must not be applied: the pin survives, and the
    // discarded 'alive' verdict must not poison the cache (next pass re-probes).
    expect($pinnedSessionIds.get()).toEqual(['race-pin'])
    getSessionMock.mockClear()
    getSessionMock.mockRejectedValue(notFound())
    $sessions.set([row('unrelated')])

    await __runDeadSessionPrunePass()

    expect(getSessionMock).toHaveBeenCalledWith('race-pin', undefined)
    expect($pinnedSessionIds.get()).toEqual([])
  })

  it('resetDeadSessionPrune drops the alive cache so a switched gateway is re-probed', async () => {
    $sessions.set([row('unrelated')])
    $pinnedSessionIds.set(['switched-pin'])
    getSessionMock.mockImplementation(async id => row(id, { profile: 'default' }))

    await __runDeadSessionPrunePass()
    expect($pinnedSessionIds.get()).toEqual(['switched-pin'])

    // The gateway switched; the new backend no longer has the session. Without
    // the reset the alive verdict would keep the dead pin forever.
    resetDeadSessionPrune()
    getSessionMock.mockRejectedValue(notFound())

    await __runDeadSessionPrunePass()

    expect($pinnedSessionIds.get()).toEqual([])
  })
})

describe('watchDeadSessionPrune', () => {
  it('sweeps once after the first list payload', async () => {
    vi.useFakeTimers()
    const unsubscribe = watchDeadSessionPrune()

    try {
      $pinnedSessionIds.set(['timed-pin'])
      getSessionMock.mockRejectedValue(notFound())

      $sessions.set([row('boot-row')])

      vi.advanceTimersByTime(2_000)
      await vi.runOnlyPendingTimersAsync()

      expect($pinnedSessionIds.get()).toEqual([])
    } finally {
      unsubscribe()
      vi.useRealTimers()
    }
  })

  it('clamps the debounce so list churn cannot starve the sweep', async () => {
    vi.useFakeTimers()
    const unsubscribe = watchDeadSessionPrune()

    try {
      $pinnedSessionIds.set(['churn-pin'])
      getSessionMock.mockRejectedValue(notFound())

      // First payload schedules the pass at +2s; a change 1s later would
      // naively re-schedule to +6s. The clamp keeps the earlier deadline —
      // and must not stack a second timer for it.
      $sessions.set([row('a')])
      vi.advanceTimersByTime(1_000)
      $sessions.set([row('a'), row('b')])
      // Real owner stores persist on list changes; jsdom dispatches their
      // storage events at 0ms. Count only timers left after that delivery.
      vi.advanceTimersByTime(0)
      expect(vi.getTimerCount()).toBe(1)

      vi.advanceTimersByTime(1_000)
      await vi.runOnlyPendingTimersAsync()

      expect($pinnedSessionIds.get()).toEqual([])
      expect(vi.getTimerCount()).toBe(0)
    } finally {
      unsubscribe()
      vi.useRealTimers()
    }
  })

  it('re-opens a fresh debounce window after a sweep fires', async () => {
    vi.useFakeTimers()
    const unsubscribe = watchDeadSessionPrune()

    try {
      // First sweep fires at +2s and prunes the boot dead pin.
      $pinnedSessionIds.set(['first-pin'])
      getSessionMock.mockRejectedValue(notFound())
      $sessions.set([row('a')])
      vi.advanceTimersByTime(2_000)
      await vi.runOnlyPendingTimersAsync()
      expect($pinnedSessionIds.get()).toEqual([])

      // A change right after the sweep must NOT fire an immediate pass — the
      // next pass waits out a fresh RERUN_DELAY window.
      $pinnedSessionIds.set(['second-pin'])
      $sessions.set([row('a'), row('c')])

      vi.advanceTimersByTime(1_000)
      expect($pinnedSessionIds.get()).toEqual(['second-pin'])

      vi.advanceTimersByTime(4_000)
      await vi.runOnlyPendingTimersAsync()
      expect($pinnedSessionIds.get()).toEqual([])
    } finally {
      unsubscribe()
      vi.useRealTimers()
    }
  })
})

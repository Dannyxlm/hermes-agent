import { act, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { setApiRequestConnection } from '@/hermes'
import { createClientSessionState } from '@/lib/chat-runtime'
import { $pinnedSessionIds } from '@/store/layout'
import { $selectedStoredSessionId, $sessions, $unlistedSessionOwnerRows, mergeSessionPage } from '@/store/session'
import { $removedSessionIds } from '@/store/session-removal'
import { $sessionStates, $sessionTiles } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { deferred } from '../../../test/deferred'

import { resolveStoredSession } from './use-session-actions/utils'
import { useSessionListActions } from './use-session-list-actions'

const transport = vi.hoisted(() => ({ api: vi.fn(), detail: vi.fn(), page: vi.fn(), epoch: 0, settled: [] as string[] }))
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  getCronJobs: vi.fn(async () => []),
  getSession: transport.detail,
  listSidebarSessions: transport.page
}))
vi.mock('@/store/gateway', async original => ({
  ...(await original<Record<string, unknown>>()),
  gatewayActivationEpoch: () => transport.epoch
}))
vi.mock('@/store/session-states', async original => ({
  ...(await original<Record<string, unknown>>()),
  getRecentlySettledSessionIds: () => transport.settled
}))

function row(id: string, overrides: Partial<SessionInfo> = {}): SessionInfo {
  return {
    id, profile: 'worker', connection_id: 'rig', source: 'tui', title: 'Human title',
    ended_at: null, input_tokens: 0, output_tokens: 0, is_active: false,
    last_active: 20, started_at: 10, message_count: 2, model: null,
    preview: null, tool_call_count: 0, ...overrides
  }
}

const page = () => ({ recents: { sessions: [] }, cron: { sessions: [] }, messaging: { sessions: [] } })

beforeEach(() => {
  transport.api.mockReset()
  transport.detail.mockReset()
  transport.page.mockReset().mockResolvedValue(page())
  transport.epoch = 0
  transport.settled = []
  vi.stubGlobal('hermesDesktop', { api: transport.api })
  setApiRequestConnection('rig')
})
afterEach(() => {
  $sessions.set([])
  $unlistedSessionOwnerRows.set([])
  $pinnedSessionIds.set([])
  $sessionStates.set({})
  $selectedStoredSessionId.set(null)
  $sessionTiles.set([])
  setApiRequestConnection(null)
  vi.unstubAllGlobals()
})

it('merge excludes explicit thread provenance, never a tui source, title or generic parent', () => {
  const worker = { ...row('worker'), thread_parent_id: 'lead' }
  const humans = [row('human', { parent_session_id: 'lead' }), row('other', { title: 'Thread: worker' })]
  expect(mergeSessionPage([worker, ...humans], [], ['worker', 'human', 'other'])).toEqual(humans)
  expect(mergeSessionPage([], [worker, ...humans], [])).toEqual(humans)
})

it.each(['pinned', 'working', 'open', 'selected', 'settled'])('refresh evicts a cached %s worker but retains its paired human', async reason => {
  const workers = [row('worker'), row('human', { parent_session_id: 'lead' })]
  $sessions.set(workers)
  const ids = workers.map(s => s.id)

  if (reason === 'pinned') { $pinnedSessionIds.set(ids) }

  if (reason === 'working') {
    $sessionStates.set(Object.fromEntries(ids.map(id => [id, { ...createClientSessionState(id), busy: true }])))
  }

  if (reason === 'selected') {
    $selectedStoredSessionId.set('worker')
    $pinnedSessionIds.set(['human'])
  }

  if (reason === 'settled') { transport.settled = ids }

  if (reason === 'open') { $sessionTiles.set(ids.map(storedSessionId => ({ storedSessionId }))) }
  transport.api.mockResolvedValue({ profile: 'worker', thread_session_ids: ['worker'] })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual([workers[1]])
  expect($unlistedSessionOwnerRows.get()).toEqual([{ ...workers[0], _thread_list_excluded: true }])
  expect(await resolveStoredSession('worker', { connectionId: 'rig', profile: 'worker' })).toBe($unlistedSessionOwnerRows.get()[0])
  expect($sessions.get()).toEqual([workers[1]])
  expect($removedSessionIds.get().size).toBe(0)
  expect(transport.api).toHaveBeenCalledTimes(1)
})

it('keeps a human on another connection when a same-id page arrives and the worker is excluded', async () => {
  const worker = row('same')
  const human = row('same', { connection_id: 'elsewhere', title: 'My conversation' })
  $sessions.set([worker, human])
  $pinnedSessionIds.set(['same'])
  transport.page.mockResolvedValue({ ...page(), recents: { sessions: [row('new', { last_active: 30 })] } })
  transport.api.mockImplementation(async request => ({ profile: request.profile,
    thread_session_ids: request.connectionId === 'rig' ? ['same'] : [] }))
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual([row('new', { last_active: 30 }), human])
})

it('does not absorb a kept twin into another connection\'s incoming compression lineage', () => {
  const human = row('same', { connection_id: 'elsewhere' })
  const incoming = row('new', { _lineage_ids: ['same', 'new'] })
  expect(mergeSessionPage([human], [incoming], ['same'])).toEqual([human, incoming])
})

it('qualifies membership by exact profile, connection and stored id, not lineage aliases', async () => {
  const worker = row('same', { _lineage_root_id: 'root', _lineage_ids: ['root', 'same'] })
  const twins = [row('same', { profile: 'other' }), row('same', { connection_id: 'elsewhere' }), row('root')]
  $sessions.set([worker, ...twins])
  $pinnedSessionIds.set(['same', 'root'])
  transport.api.mockImplementation(async request => {
    const url = new URL(request.path, 'http://test')

    return { profile: url.searchParams.get('profile'), thread_session_ids: request.connectionId === 'rig' && request.profile === 'worker' ? ['same'] : [] }
  })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'all' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual(twins)
  expect($unlistedSessionOwnerRows.get()).toEqual([{ ...worker, _thread_list_excluded: true }])
})

it.each([undefined, {}, { id: 'list-membership', profile: 'worker', thread_parent_id: 'lead' }, { profile: 'wrong', thread_session_ids: ['worker'] }, { profile: 'worker', thread_session_ids: ['worker', 'unrequested'] }, { profile: 'worker', thread_session_ids: ['worker', null] }, { profile: 'worker', thread_session_ids: 'worker' }])('preserves legacy cache for a missing/malformed membership response %j', async response => {
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockResolvedValue(response)
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual([worker])
})

it('preserves legacy cache after a membership 404 without probing individual details', async () => {
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockRejectedValue(new Error('404: Session not found'))
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual([worker])
  expect(transport.api).toHaveBeenCalledTimes(1)
})

it.each(['profile', 'connection'])('does not publish a stale membership reply after a %s switch', async switched => {
  const pending = deferred<unknown>()
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockReturnValueOnce(pending.promise)

  const { result, rerender } = renderHook(({ profileScope }) => useSessionListActions({ profileScope }), {
    initialProps: { profileScope: 'worker' }
  })

  let refresh!: Promise<void>
  act(() => { refresh = result.current.refreshSessions() })
  await vi.waitFor(() => expect(transport.api).toHaveBeenCalledTimes(1))

  if (switched === 'profile') { rerender({ profileScope: 'other' }) }
  else { setApiRequestConnection('elsewhere') }

  await act(async () => { pending.resolve({ profile: 'worker', thread_session_ids: ['worker'] }); await refresh })
  expect($sessions.get()).toEqual([worker])
  expect($unlistedSessionOwnerRows.get()).toEqual([])
})

it('evicts explicit provenance on refresh without probing, preserving human null provenance', async () => {
  const worker = row('worker', { thread_parent_id: 'lead' })
  const human = row('human', { thread_parent_id: null, parent_session_id: 'lead' })
  $sessions.set([worker, human])
  $pinnedSessionIds.set(['worker', 'human'])
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  expect($sessions.get()).toEqual([human])
  expect($unlistedSessionOwnerRows.get()).toEqual([{ ...worker, _thread_list_excluded: true }])
  expect(transport.api).not.toHaveBeenCalled()
})

it('discards membership from an obsolete refresh generation', async () => {
  const pending = deferred<unknown>()
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockReturnValueOnce(pending.promise).mockResolvedValue({ profile: 'worker', thread_session_ids: [] })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  let old!: Promise<void>
  act(() => { old = result.current.refreshSessions() })
  await vi.waitFor(() => expect(transport.api).toHaveBeenCalledTimes(1))
  await act(() => result.current.refreshSessions())
  await act(async () => { pending.resolve({ profile: 'worker', thread_session_ids: ['worker'] }); await old })
  expect($sessions.get()).toEqual([worker])
  expect($unlistedSessionOwnerRows.get()).toEqual([])
})

it('does not reinsert an older unmarked detail reply after membership proved the worker off-list', async () => {
  const pending = deferred<SessionInfo>()
  transport.detail.mockReturnValueOnce(pending.promise)
  const resolved = resolveStoredSession('worker', { connectionId: 'rig', profile: 'worker' })
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockResolvedValue({ profile: 'worker', thread_session_ids: ['worker'] })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  pending.resolve(worker)
  await expect(resolved).resolves.toMatchObject({ id: 'worker' })
  expect($sessions.get()).toEqual([])
  expect($unlistedSessionOwnerRows.get()).toHaveLength(1)
})

it.each([
  { connectionId: 'elsewhere', profile: 'worker' },
  { connectionId: 'rig', profile: 'other' }
])('does not apply worker membership to a late human detail on %j', async owner => {
  const pending = deferred<SessionInfo>()
  transport.detail.mockReturnValueOnce(pending.promise)
  const resolved = resolveStoredSession('same', owner)
  $sessions.set([row('same')])
  $pinnedSessionIds.set(['same'])
  transport.api.mockResolvedValue({ profile: 'worker', thread_session_ids: ['same'] })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  await act(() => result.current.refreshSessions())
  pending.resolve(row('same', { profile: owner.profile, connection_id: owner.connectionId }))
  await resolved
  expect($sessions.get()).toEqual([row('same', { profile: owner.profile, connection_id: owner.connectionId })])
})

it('re-reads page and membership after reconnect during membership await', async () => {
  const pending = deferred<unknown>()
  const worker = row('worker')
  $sessions.set([worker])
  $pinnedSessionIds.set(['worker'])
  transport.api.mockReturnValueOnce(pending.promise).mockResolvedValue({ profile: 'worker', thread_session_ids: ['worker'] })
  const { result } = renderHook(() => useSessionListActions({ profileScope: 'worker' }))
  let refresh!: Promise<void>
  act(() => { refresh = result.current.refreshSessions() })
  await vi.waitFor(() => expect(transport.api).toHaveBeenCalledTimes(1))
  transport.epoch++
  await act(async () => { pending.resolve({ profile: 'worker', thread_session_ids: [] }); await refresh })
  expect(transport.page).toHaveBeenCalledTimes(2)
  expect(transport.api).toHaveBeenCalledTimes(2)
  expect($sessions.get()).toEqual([])
})

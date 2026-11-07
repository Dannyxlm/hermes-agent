import { act, cleanup, renderHook } from '@testing-library/react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { deferred } from '@/test/deferred'

import type { FamilyTarget } from './actions'
import { useFamily } from './use-family'

const read = vi.hoisted(() => vi.fn())
vi.mock('./actions', () => ({ requestFamily: read }))
const target: FamilyTarget = { sessionId: 'parent', owner: { connectionId: 'box', profile: 'default', mode: 'remote' } }

const empty = (title: string) => ({ protocol_version: 1,
  root: { title, node_id: 'parent', parent_node_id: null, profile: 'default', session_id: 'parent', full_title: null,
    kind: 'session', status: 'working', activity_text: null, actions: ['open'], needs_you: null },
  nodes: [], complete: true, unavailable_profiles: [], truncated: false })

const overview = { protocol_version: 1, roots: [], earlier: [], complete: true, unavailable_profiles: [] }

beforeEach(() => {
  vi.useFakeTimers()
  vi.spyOn(window.document, 'visibilityState', 'get').mockReturnValue('visible')
  read.mockReset().mockResolvedValue(empty('Initial'))
})
afterEach(() => { cleanup(); vi.restoreAllMocks(); vi.useRealTimers() })

it('refreshes snapshots on the clock, skips hidden windows, resumes, and cancels on unmount', async () => {
  const { result, unmount } = renderHook(() => useFamily(target, false, 0))
  await act(async () => {})
  expect(result.current).toMatchObject({ kind: 'ready', view: { root: { title: 'Initial' } } })
  read.mockResolvedValue(empty('Worker finished'))
  await act(() => vi.advanceTimersByTimeAsync(10_000))
  expect(result.current).toMatchObject({ kind: 'ready', view: { root: { title: 'Worker finished' } } })
  vi.spyOn(window.document, 'visibilityState', 'get').mockReturnValue('hidden')
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(read).toHaveBeenCalledTimes(2)
  vi.spyOn(window.document, 'visibilityState', 'get').mockReturnValue('visible')
  await act(() => vi.advanceTimersByTimeAsync(10_000))
  expect(read).toHaveBeenCalledTimes(3)
  unmount()
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(read).toHaveBeenCalledTimes(3)
})

it.each(['resolve', 'reject'])('invalidates ready actions and ignores a previous owner\'s late %s after retargeting', async outcome => {
  const late = deferred<unknown>()
  const next = { ...target, sessionId: 'other', owner: { ...target.owner, connectionId: 'other-box' } }

  const { result, rerender, unmount } = renderHook(({ owner, all, revision }) => useFamily(owner, all, revision),
    { initialProps: { owner: target, all: false, revision: 0 } })

  await act(async () => {})
  read.mockReturnValueOnce(late.promise)
  rerender({ owner: target, all: false, revision: 1 })
  expect(result.current.kind).toBe('loading')
  read.mockResolvedValue(overview)
  rerender({ owner: next, all: true, revision: 1 })
  expect(result.current.kind).toBe('loading')
  await act(async () => {})
  expect(result.current).toMatchObject({ kind: 'ready', view: { nodes: [] } })
  await act(async () => {
    if (outcome === 'resolve') { late.resolve(empty('Stale')) } else { late.reject(new Error('Old owner offline')) }
  })
  expect(result.current).toMatchObject({ kind: 'ready', view: { nodes: [] } })
  await act(() => vi.advanceTimersByTimeAsync(10_000))
  expect(read).toHaveBeenLastCalledWith(next, true)
  expect(read.mock.calls.filter(([owner]) => owner === target)).toHaveLength(2)
  const pending = deferred<unknown>()
  read.mockReturnValueOnce(pending.promise)
  await act(() => vi.advanceTimersByTimeAsync(10_000))
  unmount()
  const count = read.mock.calls.length
  await act(async () => pending.resolve(overview))
  await act(() => vi.advanceTimersByTimeAsync(30_000))
  expect(read).toHaveBeenCalledTimes(count)
})

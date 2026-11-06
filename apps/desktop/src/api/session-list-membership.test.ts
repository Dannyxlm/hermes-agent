import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { SessionInfo } from '@/types/hermes'

import { setApiRequestConnection } from './client'
import { readThreadListExclusions } from './session-list-membership'

const api = vi.fn()

const row = (id: string, overrides: Partial<SessionInfo> = {}): SessionInfo => ({
  id, profile: 'default', source: 'tui', title: null, preview: null, ended_at: null,
  input_tokens: 0, output_tokens: 0, is_active: false, last_active: 0,
  started_at: 0, message_count: 1, tool_call_count: 0, model: null, ...overrides
})

beforeEach(() => {
  api.mockReset()
  vi.stubGlobal('hermesDesktop', { api })
  setApiRequestConnection('ambient-not-owner')
})

afterEach(() => { setApiRequestConnection(null); vi.unstubAllGlobals() })

it('chunks only exact kept ids at 100 and pins default and every exact owner', async () => {
  const rows = Array.from({ length: 201 }, (_, i) => row(`stored-${i}`))
  rows.push(row(' twin ', { connection_id: 'other', profile: 'work' }))
  api.mockImplementation(async request => {
    const params = new URL(request.path, 'http://test').searchParams
    const ids: string[] = JSON.parse(params.get('ids') || '[]')
    expect(request.method).toBe('GET')
    expect(request.priority).toBeUndefined()
    expect(request.profile).toBe(params.get('profile'))
    expect(ids.length).toBeLessThanOrEqual(100)

    return { profile: request.profile, thread_session_ids: [ids[0]] }
  })
  const excluded = await readThreadListExclusions(rows, 'rig', () => true)
  expect(api).toHaveBeenCalledTimes(4)
  expect(api.mock.calls.map(([r]) => r.connectionId)).toEqual(['rig', 'rig', 'rig', 'other'])
  expect(api.mock.calls.map(([r]) => JSON.parse(new URL(r.path, 'http://test').searchParams.get('ids') || '[]').length)).toEqual([100, 100, 1, 1])
  expect(excluded.size).toBe(4)
  expect(JSON.parse(new URL(api.mock.calls[3][0].path, 'http://test').searchParams.get('ids') || '[]')).toEqual([' twin '])
})

it('makes no detail calls for known provenance or invalid stored ids', async () => {
  const excluded = await readThreadListExclusions([
    row('known', { thread_parent_id: 'lead' }), row('human', { thread_parent_id: null }),
    row(''), row('x'.repeat(257))
  ], null, () => true)

  expect(excluded.size).toBe(1)
  expect(api).not.toHaveBeenCalled()
})

it('never falls back to an ambient backend for an explicitly local owner', async () => {
  api.mockResolvedValue({ profile: 'default', thread_session_ids: ['stored'] })
  expect((await readThreadListExclusions([row('stored', { connection_id: 'local' })], 'rig', () => true)).size).toBe(1)
  expect(api).toHaveBeenCalledTimes(1)
  expect(api.mock.calls[0][0]).toMatchObject({ connectionId: 'local', profile: 'default' })
})

it('does not start or continue requests when the route fence is lost', async () => {
  let owns = false
  expect((await readThreadListExclusions([row('stored')], 'rig', () => owns)).size).toBe(0)
  expect(api).not.toHaveBeenCalled()
  owns = true
  api.mockImplementation(async () => { owns = false;

 return { profile: 'default', thread_session_ids: ['stored'] } })
  expect((await readThreadListExclusions(Array.from({ length: 101 }, (_, i) => row(i ? `s${i}` : 'stored')), 'rig', () => owns)).size).toBe(0)
  expect(api).toHaveBeenCalledTimes(1)
})

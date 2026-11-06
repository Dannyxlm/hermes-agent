import { beforeEach, expect, test, vi } from 'vitest'

import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import * as gateway from '@/store/gateway'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'

import { discardLostPrompt } from './discard-lost-prompt'

const barrier = { admission_id: 'lost', ref: { profile_id: '/opaque/owner-home', session_id: 'chat' },
  status: 'unknown', execution_generation: 4, controls: ['resolve_unknown'] }

const pending = { admission_id: 'lost', input_id: 'prior-input', status: 'unknown', user: 'prior input', execution_generation: 4, controls: [] }

beforeEach(() => {
  window.localStorage.clear()
  $queuedPromptsBySession.set({})
  $sessions.set([])
  $connection.set(null)
  $activeGatewayProfile.set('default')
})

test('legacy empty discovery revokes controls but a verified same-generation barrier recovers', () => {
  const info = { stored_session_id: 'chat', profile_name: 'default', blocking_unknown: [barrier] }
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  applyRuntimeInfo({ ...info, blocking_unknown: [] })
  expect(getQueuedPrompts('chat')).toEqual([])
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'lost', serverControls: ['resolve_unknown'] }])
})

test('legacy authenticated named route binds an opaque home ref and preserves exact terminal identity', async () => {
  $activeGatewayProfile.set('alpha')
  $sessions.set([{ id: 'chat', profile: 'alpha' }] as never)
  const info = { stored_session_id: 'chat', profile_name: 'alpha', blocking_unknown: [barrier] }
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'lost', observedUnknown: { ref: barrier.ref } }])

  const request = vi.fn(async (method: string) => method === 'prompt.resolve_unknown'
    ? { ...barrier, status: 'terminal' } : { ...info, blocking_unknown: [] })

  const ownerRequest = vi.spyOn(gateway, 'requestGatewayForProfile').mockImplementation(async (_profile, method) => request(method) as never)

  try {
    await discardLostPrompt('ui-runtime', 'chat', 'lost', request as never, 4)
    expect(ownerRequest).toHaveBeenCalledWith('alpha', 'prompt.resolve_unknown', { session_id: 'ui-runtime', admission_id: 'lost', execution_generation: 4 }, undefined, undefined)
    expect(ownerRequest).toHaveBeenLastCalledWith('alpha', 'session.info', { session_id: 'ui-runtime' }, undefined, undefined)
  } finally { ownerRequest.mockRestore() }

  expect(getQueuedPrompts('chat')).toEqual([])
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toEqual([])
})

test('foreign legacy profile metadata cannot grant a barrier on the captured route', () => {
  applyRuntimeInfo({ stored_session_id: 'chat', profile_name: 'foreign', blocking_unknown: [barrier] })
  expect(getQueuedPrompts('chat')).toEqual([])
})

test('verified controls dedupe a self-submitted unknown without losing its input or attachments', () => {
  const attachments = [{ id: 'file', name: 'evidence.txt', kind: 'file', path: '/uploads/evidence.txt' }] as never
  enqueueQueuedPrompt('chat', { id: 'prior-input', text: 'actual question', attachments })
  applyRuntimeInfo({ stored_session_id: 'chat', pending_submissions: [{ ...pending, user: 'actual question' }], blocking_unknown: [barrier] })
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'lost', text: 'actual question', attachments, submissionId: 'prior-input',
    serverControls: ['resolve_unknown'], serverExecutionGeneration: 4 }])
  applyRuntimeInfo({ stored_session_id: 'chat', pending_submissions: [pending], blocking_unknown: [barrier] })
  expect(getQueuedPrompts('chat')).toHaveLength(1)
  expect(getQueuedPrompts('chat')[0]).toMatchObject({ text: 'actual question', attachments, submissionId: 'prior-input' })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [] })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  expect(getQueuedPrompts('chat')[0]).toMatchObject({ text: 'actual question', attachments, submissionId: 'prior-input' })
})

test('a different earliest barrier revokes, but never terminally retires, an absent admission', () => {
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [{ ...barrier, admission_id: 'next' }] })
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'lost', serverControls: ['resolve_unknown'] }])
})

test('only exact raw-profile, admission and generation terminal metadata beats a stale resume', () => {
  const info = { stored_session_id: 'chat', blocking_unknown: [barrier] }
  applyRuntimeInfo(info)
  applyRuntimeInfo({ ...info, blocking_unknown: [{ ...barrier, status: 'terminal', controls: [],
    ref: { ...barrier.ref, profile_id: '/opaque/foreign-home' } }] })
  expect(getQueuedPrompts('chat')).toHaveLength(1)

  for (const execution_generation of [3, 5]) {
    applyRuntimeInfo({ ...info, blocking_unknown: [{ ...barrier, status: 'terminal', controls: [], execution_generation }] })
    expect(getQueuedPrompts('chat')).toHaveLength(1)
  }

  applyRuntimeInfo({ ...info, blocking_unknown: [{ ...barrier, status: 'terminal', controls: [] }] })
  expect(getQueuedPrompts('chat')).toEqual([])
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toEqual([])
})

test('a terminal receipt from a different raw home cannot retire the held legacy barrier', async () => {
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  await discardLostPrompt('ui-runtime', 'chat', 'lost', vi.fn(async () => ({ ...barrier, status: 'terminal',
    ref: { ...barrier.ref, profile_id: '/opaque/foreign-home' } })) as never, 4)
  expect(getQueuedPrompts('chat')).toHaveLength(1)
})

test('Discard never substitutes a canonical reference for a missing UI runtime id', async () => {
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  const request = vi.fn(async () => ({}))
  await expect(discardLostPrompt(null, 'chat', 'lost', request as never, 4)).rejects.toThrow()
  expect(request).not.toHaveBeenCalled()
  expect(getQueuedPrompts('chat')).toHaveLength(1)
})

test('successful explicit Discard refreshes info on the same UI runtime to reveal the next barrier, without replay', async () => {
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })

  const request = vi.fn(async (method: string) => method === 'prompt.resolve_unknown'
    ? { ...barrier, status: 'terminal', outcome: 'interrupted' }
    : { stored_session_id: 'chat', pending_submissions: [], blocking_unknown: [{ ...barrier, admission_id: 'next', execution_generation: 7 }] })

  await discardLostPrompt('ui-runtime', 'chat', 'lost', request as never, 4)
  expect(request.mock.calls.map(([method]) => method)).toEqual(['prompt.resolve_unknown', 'session.info'])
  expect(request).toHaveBeenLastCalledWith('session.info', { session_id: 'ui-runtime' })
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'next', serverExecutionGeneration: 7 }])
})

test('terminal Discard wins over a stale concurrent resume listing both pending and blocking for the same admission', async () => {
  const info = { stored_session_id: 'chat', pending_submissions: [pending], blocking_unknown: [barrier] }
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'lost', serverControls: ['resolve_unknown'] }])
  await discardLostPrompt('ui-runtime', 'chat', 'lost', vi.fn(async () => ({ ...barrier, status: 'terminal', outcome: 'interrupted' })) as never, 4)
  applyRuntimeInfo(info)
  expect(getQueuedPrompts('chat')).toEqual([])
})

test('owner-verified empty controls override a duplicate pending Discard grant', () => {
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [{ ...barrier, controls: [] }],
    pending_submissions: [{ ...pending, controls: ['resolve_unknown'] }] })
  expect(getQueuedPrompts('chat').every(entry => !entry.serverControls?.includes('resolve_unknown'))).toBe(true)
})

test('a stale callback from a former connection cannot discard a newly served same-id/generation barrier', async () => {
  $connection.set({ connectionId: 'A' } as never)
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  const old = getQueuedPrompts('chat')[0]
  $connection.set({ connectionId: 'B' } as never)
  applyRuntimeInfo({ stored_session_id: 'chat', blocking_unknown: [barrier] })
  const request = vi.fn(async () => ({}))
  await expect(discardLostPrompt('ui-runtime', 'chat', 'lost', request as never, 4, old)).rejects.toThrow()
  expect(request).not.toHaveBeenCalled()
})

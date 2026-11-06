import { beforeEach, expect, test, vi } from 'vitest'

import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import { $queuedPromptsBySession, getQueuedPrompts } from '@/store/composer-queue'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'

import { discardLostPrompt } from './discard-lost-prompt'

const barrier = { admission_id: 'lost', ref: { profile_id: 'default', session_id: 'chat' },
  status: 'unknown', execution_generation: 4, controls: ['resolve_unknown'] }

const pending = { admission_id: 'lost', input_id: 'prior-input', status: 'unknown', user: 'prior input', execution_generation: 4, controls: [] }

beforeEach(() => {
  window.localStorage.clear()
  $queuedPromptsBySession.set({})
  $sessions.set([])
  $connection.set(null)
  $activeGatewayProfile.set('default')
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

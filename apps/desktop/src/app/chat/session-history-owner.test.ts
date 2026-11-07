import { afterEach, expect, it } from 'vitest'

import { $sessions, _resetSessionOwnerHintsForTests, setSessionOwnerHint } from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'
import { makeSessionInfo } from '@/test/session-info'

import { sessionHistoryOwner } from './session-history-owner'

afterEach(() => { clearAllSessionStates(); $sessions.set([]); _resetSessionOwnerHintsForTests({ storage: true }) })
it('keeps a displayed original row on its exact backend despite a remembered same-ID sibling', () => {
  const original = { connectionId: 'box', profile: 'dial-alias', targetProfile: 'default' }
  setSessionOwnerHint('shared', original)
  setSessionOwnerHint('shared', { ...original, targetProfile: 'reviewer' })
  $sessions.set([makeSessionInfo({ id: 'shared', connection_id: 'box', profile: 'default' })])
  expect(sessionHistoryOwner('shared', null)).toEqual({ ownerRoute: original, ambiguous: false })
})
it('marks an off-list same-ID ambiguity unavailable instead of borrowing the foreground connection', () => {
  const first = { connectionId: 'box', profile: 'dial-alias', targetProfile: 'default' }
  setSessionOwnerHint('shared', first)
  setSessionOwnerHint('shared', { ...first, targetProfile: 'reviewer' })
  expect(sessionHistoryOwner('shared', null)).toEqual({ ownerRoute: undefined, ambiguous: true })
})

it('does not choose a first-hit row when two backend profiles expose the same stored ID', () => {
  $sessions.set([makeSessionInfo({ id: 'shared', profile: 'default' }), makeSessionInfo({ id: 'shared', profile: 'reviewer' })])
  expect(sessionHistoryOwner('shared', null)).toEqual({ ownerRoute: undefined, ambiguous: true })
})

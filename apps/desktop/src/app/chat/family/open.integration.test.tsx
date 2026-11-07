// The real app loads the SDK at boot, before an asynchronous Family click.
import '@/sdk'

import { act, renderHook, waitFor } from '@testing-library/react'
import { useEffect } from 'react'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { resumeAndBindSessionTile } from '@/app/chat/session-tile-resume'
import { useSessionTileDelegate } from '@/app/contrib/hooks/use-session-tile-delegate'
import { createSessionRpcDispatcher } from '@/app/contrib/session-rpc-dispatcher'
import { useSessionStateCache } from '@/app/session/hooks/use-session-state-cache'
import { closeSecondaryGateways, configureGatewayRegistry, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $profiles } from '@/store/profile'
import { $selectedStoredSessionId, $sessions, _resetSessionOwnerHintsForTests, getSessionOwnerHint, hydrateSessionOwnerHints, setSessionOwnerHint } from '@/store/session'
import { $sessionTiles, clearAllSessionStates, knownOwnerForSession, sessionTileDelegate } from '@/store/session-states'
import { FamilyWireSocket } from '@/test/family-wire-socket'
import type { SessionInfo } from '@/types/hermes'

import { familyChatTarget, type FamilyTarget, openFamilyChat } from './actions'

const target: FamilyTarget = { sessionId: 'unhydrated-stored-child', owner: {
  connectionId: 'owning-box', profile: 'desktop-door', targetProfile: 'reviewer', mode: 'remote'
} }

const human: SessionInfo = { id: 'human', profile: 'default', source: 'tui', title: 'Human chat',
  ended_at: null, input_tokens: 0, output_tokens: 0, is_active: false, last_active: 20, started_at: 10,
  message_count: 2, model: null, preview: null, tool_call_count: 0 }

const nativeApi = vi.fn(async () => ({ messages: [{ role: 'assistant', content: 'Stored child transcript', id: 1 }], total: 1 }))

const dial = vi.fn(async () => ({ connectionId: 'owning-box', profile: 'desktop-door', backendProfile: 'default', mode: 'remote',
  baseUrl: 'https://owning-box.invalid', wsUrl: 'wss://owning-box.invalid/api/ws', token: '', logs: [],
  isFullscreen: false, nativeOverlayWidth: 0, windowButtonPosition: null }))

beforeEach(() => {
  clearAllSessionStates()
  $sessionTiles.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
  $sessions.set([human])
  $selectedStoredSessionId.set(human.id)
  $profiles.set([])
  FamilyWireSocket.calls = []
  FamilyWireSocket.answer = method => ({ result: method === 'session.resume'
    ? { session_id: 'logical-control', messages: [{ role: 'assistant', content: 'Stored child transcript' }], running: false, writable: true }
    : {} })
  nativeApi.mockClear()
  dial.mockClear()
  vi.stubGlobal('WebSocket', FamilyWireSocket)
  vi.stubGlobal('hermesDesktop', { getConnectionFor: dial, api: nativeApi, getConnection: vi.fn(() => { throw new Error('Alias override must not be dialed') }) })
  configureGatewayRegistry({ onEvent: vi.fn() })
  setPrimaryGateway(null, 'default')
  setPrimaryGatewayConnection({ connectionId: 'ambient-box', mode: 'remote', backendProfile: 'default' })
})
afterEach(() => {
  closeSecondaryGateways()
  clearAllSessionStates()
  $sessionTiles.set([])
  _resetSessionOwnerHintsForTests({ storage: true })
  $sessions.set([])
  $selectedStoredSessionId.set(null)
  vi.unstubAllGlobals()
})

function mountRealHydration() {
  return renderHook(() => {
    const cache = useSessionStateCache({ activeSessionId: null, selectedStoredSessionId: human.id,
      busyRef: { current: false }, setAwaitingResponse: () => {}, setBusy: () => {}, setMessages: () => {} })

    const request = createSessionRpcDispatcher({ ...cache, ambientRequest: async () => { throw new Error('Ambient RPC forbidden') } })
    useSessionTileDelegate({ ...cache, requestGateway: request, archiveSession: async () => {}, branchStoredSession: async () => {},
      branchLoadedSession: async () => false, executeSlashCommand: async () => false, removeSession: async () => {} })
    // Drive the same binding action as SessionTile after core open creates a pane.
    useEffect(() => {
      const pending = new Set<string>()

      return $sessionTiles.subscribe(tiles => {
        for (const tile of tiles) {
          const delegate = sessionTileDelegate()

          if (tile.runtimeId || pending.has(tile.storedSessionId) || !delegate) { continue }
          pending.add(tile.storedSessionId)
          void resumeAndBindSessionTile(tile.storedSessionId, delegate).finally(() => pending.delete(tile.storedSessionId))
        }
      })
    }, [])

    return { cache, request }
  })
}

it.each(['clarify', 'approval'] as const)('answers %s by opening an off-list compressed child through the real SDK and routing profile-less controls without promoting Chats', async kind => {
  const destination = familyChatTarget({ sessionId: 'lead', owner: { ...target.owner, targetProfile: 'default' } }, {
    node_id: 'child', parent_node_id: 'lead', profile: 'reviewer', session_id: target.sessionId, title: 'Worker',
    full_title: null, kind: 'thread', status: 'needs_you', activity_text: null, actions: ['answer'],
    needs_you: { kind, profile: 'reviewer', session_id: 'logical-control' }
  }, true)

  expect(destination).toEqual(target)

  if (!destination) { throw new Error('Expected a stored child answer destination') }
  const humanRows = $sessions.get()
  const view = mountRealHydration()
  expect($sessionTiles.get()).toHaveLength(0)
  expect(getSessionOwnerHint(target.sessionId)).toBeUndefined()
  const opening = openFamilyChat(destination)
  await waitFor(() => expect({ calls: FamilyWireSocket.calls, tiles: $sessionTiles.get() }).toMatchObject({
    tiles: expect.arrayContaining([expect.objectContaining({ runtimeId: 'logical-control' })])
  }), { timeout: 5000 })
  await opening
  expect(dial).toHaveBeenCalledWith({ connectionId: 'owning-box', profile: 'desktop-door' })
  expect(getSessionOwnerHint(target.sessionId, target.owner)).toEqual(target.owner)
  expect($sessionTiles.get()).toEqual(expect.arrayContaining([expect.objectContaining({ storedSessionId: target.sessionId, runtimeId: 'logical-control' })]))
  expect(view.result.current.cache.sessionStateByRuntimeIdRef.current.get('logical-control')?.messages).toEqual(expect.arrayContaining([expect.objectContaining({ role: 'assistant' })]))
  const resume = FamilyWireSocket.calls.filter(call => call.method === 'session.resume')
  expect(resume.length).toBeGreaterThan(0)
  expect(resume.every(call => call.url.includes('owning-box.invalid') && call.params.profile === 'reviewer' && call.params.session_id === target.sessionId)).toBe(true)

  for (const method of ['prompt.submit', 'approval.respond', 'clarify.respond']) {
    await view.result.current.request(method, { session_id: 'logical-control', request_id: 'fixture-question', text: 'fixture text' })
    expect(FamilyWireSocket.calls.at(-1)).toMatchObject({ method, url: expect.stringContaining('owning-box.invalid'), params: { profile: 'reviewer', session_id: 'logical-control' } })
  }

  _resetSessionOwnerHintsForTests()
  hydrateSessionOwnerHints()
  expect(getSessionOwnerHint(target.sessionId, target.owner)).toEqual(target.owner)
  expect($sessions.get()).toBe(humanRows)
  expect(FamilyWireSocket.calls.some(call => /visibility|unhide|session\.create|session\.delete/.test(call.method))).toBe(false)
  expect(nativeApi.mock.calls.every(args => !/visibility|unhide/.test(JSON.stringify(args)))).toBe(true)
  view.unmount()
})

it('keeps a same-ID original and sibling hints separate and refuses an unsafe second tile before overwriting ownership', async () => {
  const original = { ...target.owner, targetProfile: 'default' }
  $sessions.set([{ ...human, id: target.sessionId, connection_id: 'owning-box' }])
  setSessionOwnerHint(target.sessionId, original)
  setSessionOwnerHint(target.sessionId, target.owner)
  expect(getSessionOwnerHint(target.sessionId)).toBeUndefined()
  expect(getSessionOwnerHint(target.sessionId, { connectionId: 'owning-box', profile: 'desktop-door' })).toBeUndefined()
  expect(getSessionOwnerHint(target.sessionId, original)).toEqual(original)
  expect(getSessionOwnerHint(target.sessionId, target.owner)).toEqual(target.owner)
  expect(knownOwnerForSession(target.sessionId)).toEqual(original)
  await expect(openFamilyChat(target)).rejects.toThrow('cannot safely open both')
  expect(dial).not.toHaveBeenCalled()
  expect(knownOwnerForSession(target.sessionId)).toEqual(original)
})

it('surfaces native open failure and retries the same captured route without modifying human Chats', async () => {
  const view = mountRealHydration()
  const humanRows = $sessions.get()
  dial.mockRejectedValueOnce(new Error('fixture dial unavailable'))
  await act(async () => { await expect(openFamilyChat(target)).rejects.toThrow('fixture dial unavailable') })
  const opening = openFamilyChat(target)
  await waitFor(() => expect($sessionTiles.get()).toEqual(expect.arrayContaining([expect.objectContaining({ runtimeId: 'logical-control' })])), { timeout: 5000 })
  await opening
  expect(dial.mock.calls.every(args => JSON.stringify(args) === JSON.stringify([{ connectionId: 'owning-box', profile: 'desktop-door' }]))).toBe(true)
  expect($sessions.get()).toBe(humanRows)
  expect(getSessionOwnerHint(target.sessionId, target.owner)).toEqual(target.owner)
  view.unmount()
})

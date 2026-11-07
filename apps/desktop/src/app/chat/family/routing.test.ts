import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import type { HermesConnection } from '@/global'
import { closeSecondaryGateways, configureGatewayRegistry, ensureGatewayForAgent, requestGatewayForAgent,
  requestGatewayForProfile, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $connection, $sessions } from '@/store/session'
import { clearAllSessionStates } from '@/store/session-states'
import type { SessionInfo } from '@/types/hermes'

import { familyTarget, openFamilyChat, requestFamily, targetForProfile } from './actions'

const transport = vi.hoisted(() => ({ wire: vi.fn(async () => ({})), open: vi.fn(async () => {}) }))
vi.mock('@/sdk', () => ({ host: { openSession: transport.open } }))
// Only native transport is substituted. Session ownership and both routing dispatchers are real.
vi.mock('@/hermes', async original => ({
  ...(await original<Record<string, unknown>>()),
  HermesGateway: class {
    connectionState = 'closed'
    connect = async () => { this.connectionState = 'open' }
    close = () => {}
    onEvent = () => () => {}
    onRequest = () => () => {}
    onState = () => () => {}
    request = transport.wire
  }
}))

const descriptor = (connectionId: string, mode: 'local' | 'remote', profile: string): HermesConnection => ({
  connectionId, mode, profile, baseUrl: 'https://fixture.invalid', wsUrl: 'wss://fixture.invalid', token: '',
  logs: [], isFullscreen: false, nativeOverlayWidth: 0, windowButtonPosition: null
})

const parent: SessionInfo = { id: 'family-routing-parent', profile: 'default', source: 'tui', title: 'Human chat',
  ended_at: null, input_tokens: 0, output_tokens: 0, is_active: false, last_active: 20, started_at: 10,
  message_count: 2, model: null, preview: null, tool_call_count: 0 }

const primary = { connectionState: 'open', request: transport.wire }

beforeEach(() => {
  vi.clearAllMocks()
  clearAllSessionStates()
  configureGatewayRegistry({ onEvent: vi.fn() })
  setPrimaryGateway(primary as never, 'default')
  setPrimaryGatewayConnection(descriptor('local', 'local', 'default'))
  $sessions.set([parent])
  vi.stubGlobal('hermesDesktop', {
    getConnection: vi.fn(async () => { throw new Error('Legacy alias override must not be consulted') }),
    getConnectionFor: vi.fn(async ({ connectionId, profile }) => ({ ...descriptor(connectionId, 'remote', profile), sharedRemote: connectionId === 'box-a' }))
  })
})
afterEach(() => {
  closeSecondaryGateways()
  $sessions.set([])
  $connection.set(null)
  clearAllSessionStates()
  vi.unstubAllGlobals()
})

it('pins a bare primary owner while a secondary is foregrounded, retaining it after another foreground change', async () => {
  await ensureGatewayForAgent('ambient-secondary', 'default')
  const target = familyTarget(parent.id)
  expect(target?.owner).toEqual({ connectionId: 'local', profile: 'default', targetProfile: 'default', mode: 'local' })

  if (!target) { throw new Error('Expected captured owner') }
  await requestFamily(target, false)
  expect(transport.wire).toHaveBeenLastCalledWith('session.family', { profile: 'default', session_id: parent.id }, 15_000, undefined)
  await ensureGatewayForAgent('another-secondary', 'default')
  await openFamilyChat(targetForProfile(target, 'reviewer', 'off-list-child'))
  expect(transport.open).toHaveBeenLastCalledWith('off-list-child', expect.objectContaining({ route: {
    connectionId: 'local', profile: 'default', targetProfile: 'reviewer', mode: 'local'
  } }))
  expect($sessions.get()).toEqual([parent])
})

it('retains the captured backend when the window primary is re-homed after the Family click', async () => {
  const target = familyTarget(parent.id)

  if (!target) { throw new Error('Expected captured owner') }
  const replacement = { connectionState: 'open', request: vi.fn(async () => ({})) }
  setPrimaryGateway(replacement as never, 'default')
  setPrimaryGatewayConnection(descriptor('replacement-box', 'remote', 'default'))
  await requestFamily(target, false)
  expect(window.hermesDesktop?.getConnectionFor).toHaveBeenCalledWith({ connectionId: 'local', profile: 'default' })
  expect(replacement.request).not.toHaveBeenCalled()
  await openFamilyChat(targetForProfile(target, 'reviewer', 'original-backend-child'))
  expect(transport.open).toHaveBeenLastCalledWith('original-backend-child', expect.objectContaining({ route: {
    connectionId: 'local', mode: 'local', profile: 'default', targetProfile: 'reviewer'
  } }))
})

it('keeps a backend child profile on the captured remote alias, never the same-named legacy override', async () => {
  setPrimaryGateway(primary as never, 'desktop-alias')
  setPrimaryGatewayConnection(descriptor('box-a', 'remote', 'default'))
  $sessions.set([{ ...parent, profile: 'desktop-alias' }])
  const target = familyTarget(parent.id)
  expect(target?.owner).toMatchObject({ connectionId: 'box-a', profile: 'desktop-alias', targetProfile: 'default' })

  if (!target) { throw new Error('Expected captured owner') }
  const child = targetForProfile(target, 'reviewer', 'compressed-tip')
  await requestFamily(child, false)
  expect(transport.wire).toHaveBeenLastCalledWith('session.family', { profile: 'reviewer', session_id: 'compressed-tip' }, 15_000, undefined)
  await requestFamily(child, true)
  expect(transport.wire).toHaveBeenLastCalledWith('family.overview', { profile: 'reviewer' }, 15_000, undefined)
  expect(window.hermesDesktop?.getConnection).not.toHaveBeenCalled()
  await openFamilyChat(child)
  expect(transport.open).toHaveBeenLastCalledWith('compressed-tip', expect.objectContaining({ route: {
    connectionId: 'box-a', profile: 'desktop-alias', targetProfile: 'reviewer', mode: 'remote'
  } }))
})

it('preserves Overview profile injection through both real shared-primary dispatchers', async () => {
  setPrimaryGatewayConnection(descriptor('box-a', 'remote', 'default'))
  vi.mocked(window.hermesDesktop!.getConnection).mockResolvedValue({ ...descriptor('box-a', 'remote', 'reviewer'), sharedPrimary: true })
  await requestGatewayForProfile('reviewer', 'family.overview', {})
  expect(transport.wire).toHaveBeenLastCalledWith('family.overview', { profile: 'reviewer' })
  $sessions.set([{ ...parent, profile: 'reviewer' }])
  expect(familyTarget(parent.id)?.owner).toMatchObject({ connectionId: 'box-a', profile: 'reviewer', targetProfile: 'reviewer' })
  await requestGatewayForAgent('box-a', 'reviewer', 'family.overview', {})
  expect(transport.wire).toHaveBeenLastCalledWith('family.overview', { profile: 'reviewer' })
  const target = familyTarget(parent.id)

  if (!target) { throw new Error('Expected shared profile owner') }
  await requestFamily(targetForProfile(target, 'another-backend-profile', 'worker-tip'), true)
  expect(transport.wire).toHaveBeenLastCalledWith('family.overview', { profile: 'another-backend-profile' }, 15_000, undefined)
})

it('fails visibly rather than borrowing a foreground source when the bare owner door is not known', () => {
  setPrimaryGatewayConnection(null)
  expect(familyTarget(parent.id)).toBeNull()
})

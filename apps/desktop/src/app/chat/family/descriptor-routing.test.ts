import { afterEach, expect, it, vi } from 'vitest'

import { HermesGateway } from '@/api/client'
import { closeSecondaryGateways, configureGatewayRegistry, ensureGatewayForProfile, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { $sessions } from '@/store/session'
import { FamilyWireSocket } from '@/test/family-wire-socket'
import type { SessionInfo } from '@/types/hermes'

import { backendProfileDescriptor, primaryRouteDescriptor } from '../../../../electron/backend-profile-descriptor'
import { createPrimaryRemoteConnection } from '../../../../electron/primary-remote-startup'

import { familyTarget } from './actions'

let primary: HermesGateway

async function nativeDescriptor(alias: string, backend: string, shared = false) {
  // Real resolver overrides run in the Electron project; these are the same
  // native constructors used after that namespace has been resolved.
  const resolved = backendProfileDescriptor({ baseUrl: 'https://box.invalid', wsUrl: 'wss://box.invalid/ws', token: '', connectionId: 'box' }, backend)

  return primaryRouteDescriptor(createPrimaryRemoteConnection(resolved, [], {
    isFullscreen: false, nativeOverlayWidth: 0, windowButtonPosition: null
  }), alias, { descriptorProfile: shared ? alias : null })
}

async function boot(alias: string, backend: string) {
  vi.stubGlobal('WebSocket', FamilyWireSocket)
  FamilyWireSocket.answer = () => ({ result: {} })
  configureGatewayRegistry({ onEvent: vi.fn() })
  primary = new HermesGateway()
  await primary.connect('wss://box.invalid/ws')
  setPrimaryGateway(primary, alias)
  setPrimaryGatewayConnection(await nativeDescriptor(alias, backend), alias)
}

afterEach(() => { primary?.close(); closeSecondaryGateways(); $sessions.set([]); vi.unstubAllGlobals() })
const row = (profile: string) => ({ id: 'descriptor-parent', profile, source: 'test' } as SessionInfo)

it('captures the actual native primary descriptor backend namespace, not its Desktop alias', async () => {
  await boot('desktop-alias', 'different-backend')
  $sessions.set([row('desktop-alias')])
  expect(familyTarget('descriptor-parent')?.owner).toEqual({ connectionId: 'box', mode: 'remote', profile: 'desktop-alias', targetProfile: 'different-backend' })
})
it('keeps a shared rider scoped to its requested backend profile on the same native primary', async () => {
  await boot('desktop-alias', 'different-backend')
  vi.stubGlobal('hermesDesktop', { getConnection: async () => nativeDescriptor('reviewer', 'different-backend', true) })
  await ensureGatewayForProfile('reviewer')
  $sessions.set([row('reviewer')])
  expect(familyTarget('descriptor-parent')?.owner).toMatchObject({ connectionId: 'box', profile: 'reviewer', targetProfile: 'reviewer' })
})
it('captures a separately dialed legacy override without confusing the alias with its backend profile', async () => {
  await boot('desktop-primary', 'primary-backend')
  vi.stubGlobal('hermesDesktop', { getConnection: async () => nativeDescriptor('desktop-secondary', 'secondary-backend') })
  await ensureGatewayForProfile('desktop-secondary')
  $sessions.set([row('desktop-secondary')])
  expect(familyTarget('descriptor-parent')?.owner).toMatchObject({ connectionId: 'box', profile: 'desktop-secondary', targetProfile: 'secondary-backend' })
})

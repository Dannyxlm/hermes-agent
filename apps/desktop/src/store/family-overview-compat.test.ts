import { afterEach, beforeEach, expect, it, vi } from 'vitest'

import { HermesGateway } from '@/api/client'
import { type FamilyTarget, requestFamily } from '@/app/chat/family/actions'
import { closeSecondaryGateways, configureGatewayRegistry, setPrimaryGateway, setPrimaryGatewayConnection } from '@/store/gateway'
import { FamilyWireSocket } from '@/test/family-wire-socket'

import { backendProfileDescriptor } from '../../electron/backend-profile-descriptor'

let gateway: HermesGateway
const target: FamilyTarget = { sessionId: 'parent', owner: { connectionId: 'box', profile: 'desk-alias', targetProfile: 'backend-home', mode: 'remote' } }
const canonicalRefusal = { code: 4001, message: 'invalid_params', data: { reason: 'invalid_params', fields: ['profile'] } }
const legacyRefusal = { code: 4000, message: 'invalid params for family.overview: profile: Extra inputs are not permitted — the client and the Hermes backend are out of sync (different versions); run `hermes update` and restart both' }

beforeEach(() => {
  vi.stubGlobal('WebSocket', FamilyWireSocket)
  FamilyWireSocket.calls = []
  configureGatewayRegistry({ onEvent: vi.fn() })
})
afterEach(() => { gateway?.close(); closeSecondaryGateways(); vi.unstubAllGlobals() })

async function connect(canonical: boolean, shared = false) {
  gateway = new HermesGateway()
  await gateway.connect(`wss://box.invalid/api/ws${canonical ? '?native_dial=1&ticket=synthetic-test-ticket' : ''}`)
  setPrimaryGateway(gateway, 'desk-alias')
  setPrimaryGatewayConnection({ ...backendProfileDescriptor({ connectionId: 'box', mode: 'remote' as const, profile: 'desk-alias' }, 'backend-home'), sharedRemote: shared })
}

it.each([false, true])('keeps new transport %s scoped and retries only a precise old profile-envelope refusal on its dedicated captured socket', async canonical => {
  await connect(canonical)
  FamilyWireSocket.answer = () => ({ result: { marker: 'new backend' } })
  expect(await requestFamily(target, true)).toEqual({ marker: 'new backend' })
  expect(FamilyWireSocket.calls.at(-1)?.params).toEqual({ profile: 'backend-home' })
  FamilyWireSocket.calls = []
  FamilyWireSocket.answer = (_method, params) => 'profile' in params
    ? { error: canonical ? canonicalRefusal : legacyRefusal } : { result: { marker: 'old backend' } }
  expect(await requestFamily(target, true)).toEqual({ marker: 'old backend' })
  expect(FamilyWireSocket.calls.map(call => call.params)).toEqual([{ profile: 'backend-home' }, {}])
  expect(new Set(FamilyWireSocket.calls.map(call => call.url)).size).toBe(1)
})

it.each([false, true])('never strips sibling or shared-socket scope on transport %s', async canonical => {
  await connect(canonical)
  FamilyWireSocket.answer = () => ({ error: canonical ? canonicalRefusal : legacyRefusal })
  await expect(requestFamily({ ...target, owner: { ...target.owner, targetProfile: 'sibling' } }, true)).rejects.toThrow()
  const overview = FamilyWireSocket.calls.filter(call => call.method === 'family.overview')
  expect(overview).toHaveLength(1)
  expect(overview[0].params).toEqual({ profile: 'sibling' })
  setPrimaryGatewayConnection({ connectionId: 'box', mode: 'remote', backendProfile: 'backend-home', sharedRemote: true })
  FamilyWireSocket.calls = []
  await expect(requestFamily(target, true)).rejects.toThrow()
  expect(FamilyWireSocket.calls).toHaveLength(1)
})

it.each([
  { code: 4001, message: 'invalid_params' },
  { code: 4001, message: 'permission_denied', data: { reason: 'permission_denied', fields: ['profile'] } },
  { code: 4001, message: 'invalid_params', data: { reason: 'invalid_params', fields: ['profile', 'since'] } },
  { code: 4000, message: 'invalid params' }
])('does not retry a generic or denied response: %j', async error => {
  await connect(false)
  FamilyWireSocket.answer = () => ({ error })
  await expect(requestFamily(target, true)).rejects.toThrow()
  expect(FamilyWireSocket.calls).toHaveLength(1)
})

it('does not infer a dedicated socket profile from an old descriptor dial alias', async () => {
  await connect(false)
  setPrimaryGatewayConnection({ connectionId: 'box', mode: 'remote' })
  FamilyWireSocket.answer = () => ({ error: legacyRefusal })
  await expect(requestFamily(target, true)).rejects.toThrow()
  expect(FamilyWireSocket.calls).toHaveLength(1)
})

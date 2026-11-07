import { expect, it } from 'vitest'

import { profileRemoteOverride, profileSshOverride, resolveProfileBackendRoute } from './connection-config'
import { normalizeRegistry } from './connection-registry'
import { resolveDesktopRemoteRoute } from './desktop-remote-route'
import { joinPrimaryBoot } from './primary-boot-join'
import { createPrimaryRemoteConnection } from './primary-remote-startup'
import { resolvedRemoteDescriptor } from './remote-backend-profile'

it.each(['ssh', 'remote'] as const)('preserves resolver-owned %s backend identity when primary/shared wrappers replace the Desktop profile label', async mode => {
  const alias = 'desktop-door'
  const backend = 'different-remote-home'

  const config = { profiles: { [alias]: mode === 'ssh'
    ? { mode, host: 'fixture@box.invalid', remoteProfile: backend }
    : { mode, url: 'https://box.invalid', remoteProfile: backend } } }

  const resolvedProfile = profileSshOverride(config, alias)?.remoteProfile || profileRemoteOverride(config, alias)?.remoteProfile
  expect(resolvedProfile).toBe(backend)

  if (!resolvedProfile) { throw new Error('Missing resolved backend identity') }
  const remoteRoute = resolveDesktopRemoteRoute({ config, profile: alias, registry: normalizeRegistry({}) })

  if (!remoteRoute) { throw new Error('Expected a resolved remote route') }
  const resolved = resolvedRemoteDescriptor({ baseUrl: 'https://box.invalid', token: '', wsUrl: 'wss://box.invalid' }, remoteRoute, config, alias)
  const route = resolveProfileBackendRoute(alias, { primaryProfile: alias, backendProfile: resolvedProfile, globalRemote: false, profileRemoteOverride: true })

  const joined = await joinPrimaryBoot({ key: alias, inheritsPrimary: false }, route, { primaryKey: () => alias,
    resolveRoute: key => resolveProfileBackendRoute(key, { primaryProfile: alias, globalRemote: true }),
    start: async () => createPrimaryRemoteConnection(resolved, [], {}) })

  expect(joined.descriptor).toMatchObject({ profile: alias, backendProfile: backend })

  const shared = await joinPrimaryBoot({ key: 'reviewer', inheritsPrimary: false }, { descriptorProfile: 'reviewer' }, {
    primaryKey: () => alias,
    resolveRoute: () => ({ descriptorProfile: 'reviewer' }), start: async () => resolved })

  expect(shared.descriptor).toMatchObject({ profile: 'reviewer', sharedPrimary: true, backendProfile: backend })
  const secondary = { ...resolved, profile: alias }
  expect(secondary).toMatchObject({ profile: alias, backendProfile: backend })
})

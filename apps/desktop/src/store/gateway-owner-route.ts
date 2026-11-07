import { registryBackendScopeKey } from '@hermes/shared'

import type { HermesConnection } from '@/global'
import type { SessionOwnerRoute } from '@/store/session-request-router'

import type { PrimaryGatewayRoute } from './gateway-primary-route'
import type { Secondary } from './gateway-socket-entry'

/** Match the dialer's precedence, never the currently foregrounded descriptor. */
export function dialedGatewayOwnerMode(connectionId: null | string, profile: string, primaryProfile: string,
  primary: PrimaryGatewayRoute, secondaries: ReadonlyMap<string, Secondary>): 'local' | 'remote' | null {
  const id = String(connectionId ?? '').trim() || null
  const key = profile.trim() || 'default'

  if (key === primaryProfile && (!id || id === primary.connectionId) && primary.mode) { return primary.mode }
  const mode = secondaries.get(registryBackendScopeKey(id, key))?.connection?.mode

  return mode === 'local' || mode === 'remote' ? mode : null
}

/** Capture only a dialed profile door. Foreground selection is not ownership. */
export function captureGatewayOwner(
  profile: string,
  primaryProfile: string,
  primary: PrimaryGatewayRoute,
  onPrimary: boolean,
  secondary: HermesConnection | null | undefined
): SessionOwnerRoute | null {
  const source = onPrimary ? primary : secondary
  const mode = source?.mode
  const connectionId = source?.connectionId || (mode === 'local' ? 'local' : null)

  if (!connectionId || !mode) { return null }

  return {
    connectionId, mode, profile,
    targetProfile: onPrimary ? (profile === primaryProfile ? primary.targetProfile ?? profile : profile) : secondary?.profile ?? profile
  }
}

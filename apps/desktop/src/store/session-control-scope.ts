import type { GatewayEvent } from '@hermes/shared'

import { $activeGatewayProfile, normalizeProfileKey } from './profile'
import { $connection, $sessions, resolveComposerSessionKey } from './session'
import type { SessionOwnerScope } from './session-request-router'
import { knownOwnerForSession } from './session-states'

/** Capture the route before an await; an answer belongs to its request, not the next pane. */
export function captureSessionControlScope(storedId: string, owner: SessionOwnerScope = knownOwnerForSession(storedId)) {
  const connection = $connection.get()

  return {
    connectionId: owner && typeof owner === 'object' ? owner.connectionId : connection?.connectionId ?? connection?.baseUrl ?? null,
    profile: normalizeProfileKey(typeof owner === 'string' ? owner : owner?.targetProfile ?? owner?.profile ?? $activeGatewayProfile.get())
  }
}

export type SessionControlScope = ReturnType<typeof captureSessionControlScope>

/** Inbound source provenance, never the owner of a colliding stored id. */
export function sessionControlScopeForEvent(event: GatewayEvent): SessionControlScope {
  const ambient = captureSessionControlScope('', null)

  return { connectionId: event.connectionId ?? ambient.connectionId, profile: normalizeProfileKey(event.profile ?? ambient.profile) }
}

export const sessionControlOwnerKey = (scope: SessionControlScope): string => JSON.stringify([scope.connectionId, scope.profile])

/** Resolve tip/root only among rows belonging to this route, never a same-id foreign twin. */
export function sessionControlKey(storedId: string, scope = captureSessionControlScope(storedId)): string {
  const rows = $sessions.get().filter(row =>
    (!row.connection_id || row.connection_id === scope.connectionId) &&
    (!row.profile || normalizeProfileKey(row.profile) === scope.profile))

  const lineage = resolveComposerSessionKey(storedId, rows) ?? storedId

  return JSON.stringify([scope.connectionId, scope.profile, lineage])
}

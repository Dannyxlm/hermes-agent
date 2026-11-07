import { forgetSessionOwnerHint, getSessionOwnerHint, sessionOwnerAmbiguous } from '@/store/session'
import { SessionOwnerResolutionError } from '@/store/session-owner-resolution'
import { type SessionOwnerRoute, sessionOwnerRouteFromRow } from '@/store/session-request-router'

import { cachedSessionRow } from './utils'

/**
 * The persisted owner hint a pathname-driven resume may still trust, repairing a stale one in
 * the map so it cannot re-poison the next resume or a session-scoped RPC dispatch.
 *
 * The cached row is the authority, with the same predicate as the click path (openStoredSession)
 * and the boot auto-restore (repairOwnerHintsForRestore). Comparing against the live foreground
 * socket is wrong: hints are minted from the ambient connection at open time, and resumeSession
 * itself moves the foreground. A connection-tagged row that agrees keeps the hint. An untagged or
 * disagreeing row drops it: older builds persisted `local` for rows that live on a remote primary
 * (#97809). No row at all keeps it: hidden sessions (Bot Chat, plugin opens) never get a sidebar
 * row, and for them the hint is the only owner record (sdk openSession). Dropping it sent a remote
 * bot's resume to the local primary.
 */
export function rememberedOwnerForResume(storedSessionId: string): SessionOwnerRoute | undefined {
  if (sessionOwnerAmbiguous(storedSessionId)) { throw new SessionOwnerResolutionError(storedSessionId, 'session.resume') }
  const row = cachedSessionRow(storedSessionId)
  const rowRoute = sessionOwnerRouteFromRow(row)

  const hint = getSessionOwnerHint(storedSessionId, rowRoute ? {
    connectionId: rowRoute.connectionId, targetProfile: rowRoute.targetProfile || rowRoute.profile
  } : undefined)

  if (!hint) {
    return undefined
  }

  if (!row || (rowRoute && hint.connectionId === rowRoute.connectionId &&
    (hint.targetProfile || hint.profile) === (rowRoute.targetProfile || rowRoute.profile))) {
    return hint
  }

  if (!hint.targetProfile) { forgetSessionOwnerHint(storedSessionId, hint) }

  return undefined
}

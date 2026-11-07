import { sessionOwnerAmbiguous } from '@/store/session'
import { knownOwnerForSession } from '@/store/session-states'

/** The displayed conversation owns history; the foreground socket and incoming events do not. */
export function sessionHistoryOwner(storedId: null | string, runtimeId: null | string) {
  const resolved = knownOwnerForSession(runtimeId || storedId)

  return {
    ownerRoute: resolved && typeof resolved === 'object' ? resolved : undefined,
    ambiguous: !resolved && Boolean(storedId && sessionOwnerAmbiguous(storedId))
  }
}

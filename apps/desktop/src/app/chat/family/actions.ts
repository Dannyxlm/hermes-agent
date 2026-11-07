import { captureGatewayOwnerForProfile } from '@/store/gateway'
import { getSessionOwnerHints, knownSessionOwner, ownerLookupSessionRows, sessionOwnerAmbiguous } from '@/store/session'
import { requestForSessionProfile, type SessionOwnerRoute } from '@/store/session-request-router'
import { knownOwnerForSession, ownerConnectionMode, storedSessionIdForRuntimeId } from '@/store/session-states'

import type { FamilyDisplayNode } from './model'

export interface FamilyTarget {
  sessionId: string
  owner: SessionOwnerRoute & { mode: 'local' | 'remote' }
}

/** Resolve once at the user's click, not after an asynchronous profile switch. */
export function familyTarget(sessionId: string): FamilyTarget | null {
  const known = knownOwnerForSession(sessionId)
  const route = typeof known === 'string' ? captureGatewayOwnerForProfile(known) : known
  const mode = ownerConnectionMode(route)
  const owner = route && mode ? { ...route, mode, targetProfile: route.targetProfile ?? route.profile } : null

  return owner ? { sessionId: storedSessionIdForRuntimeId(sessionId) ?? sessionId, owner } : null
}

export function familyProfile(target: FamilyTarget): string {
  return target.owner.targetProfile ?? target.owner.profile
}

export function targetForProfile(target: FamilyTarget, profile: string, sessionId: string): FamilyTarget {
  // Backend profile names must never select a different Desktop alias/override.
  const owner = profile === familyProfile(target)
    ? target.owner
    : { ...target.owner, targetProfile: profile }

  return { sessionId, owner }
}

export function requestFamily(target: FamilyTarget, overview: boolean): Promise<unknown> {
  return requestForSessionProfile(target.owner, <T>() => Promise.reject<T>(new Error('Family owner unavailable')),
    overview ? 'family.overview' : 'session.family', overview ? { profile: familyProfile(target) } : {
    session_id: target.sessionId, profile: familyProfile(target)
  }, 15_000)
}

export function familyChatTarget(target: FamilyTarget, node: FamilyDisplayNode, answer: boolean): FamilyTarget | null {
  if (answer) {
    // needs_you.session_id is a live control handle, not a stored transcript id.
    // Resume the stored child; the ordinary chat attach resolves its live owner and pending requests.
    return node.actions.includes('answer') && node.needs_you?.session_id && node.session_id &&
      node.needs_you.profile === node.profile
      ? targetForProfile(target, node.profile, node.session_id) : null
  }

  return node.actions.includes('open') && node.session_id
    ? targetForProfile(target, node.profile, node.session_id) : null
}

/** No mutation of visibility. Hydration, authority, approvals and steering stay on the existing chat surface. */
export async function openFamilyChat(target: FamilyTarget): Promise<void> {
  // Tile/runtime bindings are stored-id keyed. Refuse a second owner rather than
  // silently re-homing an existing same-id conversation on another backend.
  const existing = knownSessionOwner(ownerLookupSessionRows(), target.sessionId)
  const owners = [...getSessionOwnerHints(target.sessionId), ...(existing ? [existing] : [])]

  if (sessionOwnerAmbiguous(target.sessionId) || owners.some(known => {
    const owner = typeof known === 'string' ? captureGatewayOwnerForProfile(known) : known

    return !owner || owner.connectionId !== target.owner.connectionId || (owner.targetProfile || owner.profile) !== familyProfile(target)
  })) {
    throw new Error('This session ID also belongs to another backend profile. Desktop cannot safely open both under the same stored ID.')
  }

  const { host } = await import('@/sdk')
  await host.openSession(target.sessionId, {
    route: { ...target.owner, targetProfile: familyProfile(target) }, intent: 'tab', forceResume: true, awaitHydration: true
  })
}

import { activeGateway } from '@/store/gateway'
import { requestForSessionProfile, type SessionOwnerRoute } from '@/store/session-request-router'
import { knownOwnerForSession, ownerConnectionMode, storedSessionIdForRuntimeId } from '@/store/session-states'

import type { FamilyDisplayNode } from './model'

export interface FamilyTarget {
  sessionId: string
  owner: string | SessionOwnerRoute
}

/** Resolve once at the user's click, not after an asynchronous profile switch. */
export function familyTarget(sessionId: string): FamilyTarget | null {
  const owner = knownOwnerForSession(sessionId)

  return owner ? { sessionId: storedSessionIdForRuntimeId(sessionId) ?? sessionId, owner } : null
}

export function familyProfile(target: FamilyTarget): string {
  return typeof target.owner === 'string' ? target.owner : target.owner.targetProfile ?? target.owner.profile
}

export function targetForProfile(target: FamilyTarget, profile: string, sessionId: string): FamilyTarget {
  if (typeof target.owner === 'string') { return { sessionId, owner: profile } }

  const owner = profile === familyProfile(target)
    ? target.owner
    : { ...target.owner, profile, targetProfile: profile }

  return { sessionId, owner }
}

export function requestFamily(target: FamilyTarget, overview: boolean): Promise<unknown> {
  return requestForSessionProfile(target.owner, <T>(method: string, params?: Record<string, unknown>) => {
    const gateway = activeGateway()

    if (!gateway) { return Promise.reject(new Error('Gateway unavailable')) }

    return gateway.request<T>(method, params)
  }, overview ? 'family.overview' : 'session.family', overview ? {} : {
    session_id: target.sessionId, profile: familyProfile(target)
  }, 15_000)
}

export function familyChatTarget(target: FamilyTarget, node: FamilyDisplayNode, answer: boolean): FamilyTarget | null {
  if (answer) {
    return node.actions.includes('answer') && node.needs_you?.session_id
      ? targetForProfile(target, node.needs_you.profile, node.needs_you.session_id) : null
  }

  return node.actions.includes('open') && node.session_id
    ? targetForProfile(target, node.profile, node.session_id) : null
}

/** No mutation of visibility. Hydration, authority, approvals and steering stay on the existing chat surface. */
export async function openFamilyChat(target: FamilyTarget): Promise<void> {
  const { host } = await import('@/sdk')
  const mode = ownerConnectionMode(target.owner)

  if (typeof target.owner !== 'string' && !mode) { throw new Error('Session owner connection unavailable') }

  const routing = typeof target.owner === 'string'
    ? { profile: target.owner }
    : mode ? { route: { ...target.owner, targetProfile: target.owner.targetProfile ?? target.owner.profile, mode } } : {}

  await host.openSession(target.sessionId, {
    ...routing, intent: 'tab', forceResume: true, awaitHydration: true
  })
}

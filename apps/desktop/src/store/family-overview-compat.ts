import { JsonRpcGatewayError } from '@hermes/shared'

import type { HermesGateway } from '@/api/client'
import type { HermesConnection } from '@/global'

export interface FamilySocketScope { targetProfile?: string; shared?: boolean }

export function familySocketScope(connection?: Pick<HermesConnection, 'backendProfile' | 'sharedPrimary' | 'sharedRemote'> | null): FamilySocketScope {
  return { targetProfile: connection?.backendProfile, shared: Boolean(connection?.sharedPrimary || connection?.sharedRemote) }
}

/** Strict old contracts identify the rejected *profile field*, not merely invalid params.
 * Retry only on this same socket, when its resolver proves it already owns that profile.
 * Shared sockets must keep their envelope, even when a target looks like the launch name. */
function unsupportedProfile(error: unknown): boolean {
  if (!(error instanceof JsonRpcGatewayError)) { return false }

  if (error.code === 4000) {
    return /^invalid params for family\.overview: profile: Extra inputs are not permitted(?: —|$)/.test(error.message)
  }

  if (error.code !== 4001 || !error.data || typeof error.data !== 'object') { return false }
  const data = error.data

  return 'reason' in data && data.reason === 'invalid_params' && 'fields' in data &&
    Array.isArray(data.fields) && data.fields.length === 1 && data.fields[0] === 'profile'
}

export async function requestWithFamilyCompatibility<T>(
  gateway: HermesGateway,
  scope: FamilySocketScope,
  method: string,
  params: Record<string, unknown>,
  timeoutMs?: number,
  signal?: AbortSignal
): Promise<T> {
  const request = (payload: Record<string, unknown>) => timeoutMs === undefined && signal === undefined
    ? gateway.request<T>(method, payload) : gateway.request<T>(method, payload, timeoutMs, signal)

  try {
    return await request(params)
  } catch (error) {
    if (method !== 'family.overview' || !scope.targetProfile || scope.shared ||
      params.profile !== scope.targetProfile || !unsupportedProfile(error) || signal?.aborted) { throw error }

    const { profile: _profile, ...unscoped } = params

    return request(unscoped)
  }
}

import type { BlockingUnknownAdmission } from '../../../shared/src/gateway-contract.generated'

type UnknownIdentity = Pick<BlockingUnknownAdmission, 'admission_id' | 'execution_generation'> & { session_id: string }
export type UnknownRecovery = Omit<BlockingUnknownAdmission, 'status'> & { status: 'unknown' | 'terminal' }

function receiptRefMatches(ref: unknown, sessionId: string, profileMatches: (profileId: string) => boolean): ref is BlockingUnknownAdmission['ref'] {
  return Boolean(ref && typeof ref === 'object' && 'profile_id' in ref && typeof ref.profile_id === 'string' &&
    profileMatches(ref.profile_id) && 'session_id' in ref && ref.session_id === sessionId)
}

/** Adapt authenticated metadata on the captured owner route. Raw profile IDs stay opaque;
 * only the client's snapshot aliases can bind a receipt to that route. Never read input/results. */
export async function canonicalUnknownRecovery(
  candidate: UnknownIdentity,
  request: (method: string, params: Record<string, unknown>) => Promise<unknown>,
  route: { profile?: unknown },
  profileMatches: (profileId: string) => boolean
): Promise<UnknownRecovery | undefined> {
  let value: unknown

  try {
    value = await request('prompt.receipt', { session_id: candidate.session_id,
      ...(route.profile !== undefined ? { profile: route.profile } : {}), admission_id: candidate.admission_id })
  } catch {
    // Older/offline/unpermitted owners revoke the grant, never prove terminal absence.
    return undefined
  }

  if (!value || typeof value !== 'object' || !('ref' in value) || !receiptRefMatches(value.ref, candidate.session_id, profileMatches) ||
      !('admission_id' in value) || value.admission_id !== candidate.admission_id ||
      !('execution_generation' in value) || value.execution_generation !== candidate.execution_generation ||
      !('status' in value) || !['unknown', 'terminal'].includes(String(value.status)) ||
      !('controls' in value) || !Array.isArray(value.controls) ||
      value.controls.some(control => control !== 'resolve_unknown')) { return undefined }

  return { admission_id: candidate.admission_id, execution_generation: candidate.execution_generation,
    ref: { profile_id: value.ref.profile_id, session_id: candidate.session_id },
    status: value.status === 'terminal' ? 'terminal' : 'unknown',
    controls: value.status === 'unknown' && value.controls.length ? ['resolve_unknown'] : [] }
}

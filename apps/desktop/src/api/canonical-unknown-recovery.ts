import type { BlockingUnknownAdmission } from '../../../shared/src/gateway-contract.generated'

type UnknownIdentity = Pick<BlockingUnknownAdmission, 'admission_id' | 'execution_generation' | 'ref'>

function receiptRefMatches(value: unknown, expected: UnknownIdentity['ref']): boolean {
  return Boolean(value && typeof value === 'object' && 'profile_id' in value && value.profile_id === expected.profile_id &&
    'session_id' in value && value.session_id === expected.session_id)
}

/** Adapt an authenticated metadata receipt, not a canonical blocking_unknown wire field.
 * Pending status alone never grants Discard. No include_result/input-content read is sent. */
export async function canonicalUnknownRecovery(
  candidate: UnknownIdentity,
  request: (method: string, params: Record<string, unknown>) => Promise<unknown>
): Promise<BlockingUnknownAdmission | undefined> {
  let value: unknown

  try {
    value = await request('prompt.receipt', { session_id: candidate.ref.session_id,
      profile: candidate.ref.profile_id, admission_id: candidate.admission_id })
  } catch {
    // Older/offline/unpermitted owners leave the snapshot nonretryable and without a grant.
    return undefined
  }

  if (!value || typeof value !== 'object' || !('ref' in value) || !receiptRefMatches(value.ref, candidate.ref) ||
      !('admission_id' in value) || value.admission_id !== candidate.admission_id ||
      !('execution_generation' in value) || value.execution_generation !== candidate.execution_generation ||
      !('status' in value) || value.status !== 'unknown' || !('controls' in value) || !Array.isArray(value.controls) ||
      value.controls.some(control => control !== 'resolve_unknown')) { return undefined }

  return { ...candidate, status: 'unknown', controls: value.controls.length ? ['resolve_unknown'] : [] }
}

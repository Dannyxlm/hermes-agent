import type { GatewayRequest } from '@/app/session/hooks/use-prompt-actions/utils'
import { getQueuedPrompts } from '@/store/composer-queue'
import type { QueuedPromptEntry } from '@/store/composer-queue'
import { observedUnknownMatches, reconcileBlockingUnknown, retireObservedUnknown } from '@/store/pending-submissions'
import { captureSessionControlScope } from '@/store/session-control-scope'
import { requestForOwnedSession } from '@/store/session-states'

export async function discardLostPrompt(
  sessionId: string | null | undefined,
  queueSessionKey: string,
  admissionId: string,
  request: GatewayRequest,
  executionGeneration?: number,
  expectedEntry?: QueuedPromptEntry
): Promise<unknown> {
  const entry = getQueuedPrompts(queueSessionKey).find(row => row.id === admissionId)

  if (expectedEntry?.observedUnknown && !entry?.observedUnknown) { throw new Error('Observed barrier no longer served; refresh the session') }

  if (entry?.observedUnknown && (!observedUnknownMatches(queueSessionKey, entry) ||
      executionGeneration !== entry.serverExecutionGeneration || !entry.serverControls?.includes('resolve_unknown'))) {
    throw new Error('Observed barrier owner or generation changed; refresh the session')
  }

  const targetSessionId = sessionId ?? entry?.observedUnknown?.ref.session_id ?? queueSessionKey

  // The owning socket's canonical protocol supplies the UNKNOWN admission's
  // generation. The current live execution generation may already be newer.
  const result = await requestForOwnedSession(queueSessionKey, request, 'prompt.resolve_unknown', {
    session_id: targetSessionId,
    admission_id: admissionId,
    ...(executionGeneration !== undefined ? { execution_generation: executionGeneration } : {})
  })

  if (entry?.observedUnknown) { retireObservedUnknown(queueSessionKey, entry, result) }

  return result
}

/** Read the retained forwarded admission. The bridge publishes its refreshed pending projection. */
export async function refreshPendingPrompt(
  sessionId: string | null | undefined, queueSessionKey: string, admissionId: string, request: GatewayRequest
): Promise<unknown> {
  const scope = captureSessionControlScope(queueSessionKey)

  const result = await requestForOwnedSession(queueSessionKey, request, 'prompt.receipt', {
    session_id: sessionId ?? queueSessionKey, admission_id: admissionId
  })

  if (result && typeof result === 'object' && 'admission_id' in result && result.admission_id === admissionId) {
    reconcileBlockingUnknown(queueSessionKey, [result], scope, undefined, false)
  }

  return result
}

/** Stop the exact forwarded execution observed on this card, never a successor. */
export function interruptForwardedPrompt(
  sessionId: string | null | undefined, queueSessionKey: string, admissionId: string,
  executionGeneration: number, request: GatewayRequest
): Promise<unknown> {
  return requestForOwnedSession(queueSessionKey, request, 'session.interrupt', {
    session_id: sessionId ?? queueSessionKey, admission_id: admissionId, execution_generation: executionGeneration
  })
}

/** Withdraw a server-queued admission. The authority settles every waiter and its pending fanout
 *  retires the card; a row that started meanwhile is stopped in the same RPC (no follow-up
 *  session.interrupt needed). Same owner routing as discardLostPrompt. */
export async function cancelQueuedPrompt(
  sessionId: string | null | undefined,
  queueSessionKey: string,
  admissionId: string,
  request: GatewayRequest
): Promise<unknown> {
  return requestForOwnedSession(queueSessionKey, request, 'prompt.cancel', {
    session_id: sessionId ?? queueSessionKey,
    admission_id: admissionId
  })
}

/** Deliver a server-queued admission as a steer into the running turn. The authority consumes the
 *  admission (it never also runs) and its pending fanout retires the card; a refusal
 *  (admission_started, session_idle) leaves it queued. */
export async function steerQueuedPrompt(
  sessionId: string | null | undefined,
  queueSessionKey: string,
  admissionId: string,
  request: GatewayRequest
): Promise<unknown> {
  return requestForOwnedSession(queueSessionKey, request, 'prompt.steer_queued', {
    session_id: sessionId ?? queueSessionKey,
    admission_id: admissionId
  })
}

/** Replace a server-queued admission with edited text: the authority cancels it and admits the
 *  edit as a new admission at the back of the FIFO, in one write. `submissionId` is the edit's
 *  identity: a retry of an unconfirmed save reuses it, and the owner replays a committed one. */
export async function replaceQueuedPrompt(
  sessionId: string | null | undefined,
  queueSessionKey: string,
  admissionId: string,
  text: string,
  submissionId: string,
  request: GatewayRequest
): Promise<unknown> {
  return requestForOwnedSession(queueSessionKey, request, 'prompt.replace_queued', {
    session_id: sessionId ?? queueSessionKey,
    admission_id: admissionId,
    submission_id: submissionId,
    text
  })
}

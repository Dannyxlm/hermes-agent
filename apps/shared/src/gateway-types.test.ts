import { expectTypeOf, test } from 'vitest'
import type { ForwardedPendingSubmission, SessionLiveInfo } from './gateway-events.js'
import type { PendingSubmissionRow, SessionInfo } from './gateway-types.js'

test('session snapshots accept legacy receipts and null pending lists without losing named fields', () => {
  const legacy: PendingSubmissionRow = { admission_id: 'admission', status: 'queued', user: 'hello' }
  const snapshot = { pending_submissions: [legacy] } satisfies SessionInfo
  const cleared = { pending_submissions: null } satisfies SessionInfo
  expectTypeOf(snapshot).toExtend<SessionInfo>()
  expectTypeOf(cleared).toExtend<SessionInfo>()
  expectTypeOf<SessionInfo['model']>().toEqualTypeOf<SessionLiveInfo['model']>()
  expectTypeOf<SessionInfo['provider']>().toEqualTypeOf<SessionLiveInfo['provider']>()
  expectTypeOf<PendingSubmissionRow['controls']>().toEqualTypeOf<ForwardedPendingSubmission['controls'] | undefined>()
  expectTypeOf<PendingSubmissionRow['execution_generation']>().toEqualTypeOf<ForwardedPendingSubmission['execution_generation'] | undefined>()
})

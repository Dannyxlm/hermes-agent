import type { CanonicalSnapshot } from '../../../shared/src/gateway-contract.generated'

/** Older owners omit both fields; the generated producer owns their value types. */
export type SessionWriteCapability = Pick<CanonicalSnapshot, 'writable' | 'read_only_reason'>

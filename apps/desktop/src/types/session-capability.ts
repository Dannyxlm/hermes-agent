/** Additive canonical session.info/resume verdict. Older owners omit both fields. */
export interface SessionWriteCapability {
  writable?: boolean | null
  read_only_reason?: string | null
}

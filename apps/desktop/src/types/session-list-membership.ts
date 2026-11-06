/** Display membership is independent of permission to open a session. */
export interface SessionListMembership {
  /** Internal delegate children remain readable by exact id, but are not listed. */
  is_internal_child?: boolean
  /** Trusted thread-tool provenance. Null means non-thread; absent on legacy rows. */
  thread_parent_id?: null | string
  /** Exact-owner membership evidence retained only in the off-list owner cache. */
  _thread_list_excluded?: true
}

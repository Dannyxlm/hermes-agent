import type { SessionInfo } from '@/types/hermes'

/** Only explicit server provenance (detail or exact-owner membership evidence)
 *  distinguishes Family workers from human TUI chats and branches. */
export function isThreadSession(session: SessionInfo): boolean {
  return session._thread_list_excluded === true ||
    (typeof session.thread_parent_id === 'string' && session.thread_parent_id.length > 0)
}

export function sessionListOwnerKey(session: SessionInfo, ambientConnection: null | string = null): string {
  return JSON.stringify([session.connection_id ?? ambientConnection, session.profile || 'default'])
}

export function sessionListRowKey(session: SessionInfo, ambientConnection: null | string = null): string {
  return JSON.stringify([sessionListOwnerKey(session, ambientConnection), session.id])
}

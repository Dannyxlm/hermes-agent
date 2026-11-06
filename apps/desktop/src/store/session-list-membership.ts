import { isThreadSession, sessionListOwnerKey, sessionListRowKey } from '@/lib/session-list-membership'
import type { SessionInfo } from '@/types/hermes'

import { $unlistedSessionOwnerRows, sessionMatchesStoredId, setUnlistedSessionOwnerRows } from './session'

function knownThreadExclusion(row: SessionInfo, ambientConnection: null | string): boolean {
  return $unlistedSessionOwnerRows.get().some(known => isThreadSession(known) &&
    sessionListOwnerKey(known, ambientConnection) === sessionListOwnerKey(row, ambientConnection) &&
    (sessionMatchesStoredId(known, row.id) || sessionMatchesStoredId(row, known.id)))
}

/** A detail reply read before an upgrade may omit provenance. The newer
 *  exact-owner membership evidence must survive that late cache upsert. */
export function isUnlistedResolvedSession(row: SessionInfo, ambientConnection: null | string): boolean {
  return Boolean(row.hidden || isThreadSession(row) || knownThreadExclusion(row, ambientConnection))
}

/** Reuse the owner-only cache for readable Family/hidden rows. Never collapse
 *  a twin on another profile or connection, even when lineage ids coincide. */
export function rememberUnlistedSessionRows(rows: SessionInfo[], ambientConnection: null | string = null): void {
  if (!rows.length) { return }

  const owned = rows.map<SessionInfo>(row => {
    const owner = !row.connection_id && ambientConnection ? { ...row, connection_id: ambientConnection } : row

    return knownThreadExclusion(owner, ambientConnection) ? { ...owner, _thread_list_excluded: true } : owner
  })

  const keys = new Set(owned.map(row => sessionListRowKey(row, ambientConnection)))

  const lineages = new Set(owned.map(row => JSON.stringify([
    sessionListOwnerKey(row, ambientConnection), row._lineage_root_id ?? row.id
  ])))

  setUnlistedSessionOwnerRows(prev => [
    ...owned,
    ...prev.filter(row => !keys.has(sessionListRowKey(row, ambientConnection)) &&
      !lineages.has(JSON.stringify([sessionListOwnerKey(row, ambientConnection), row._lineage_root_id ?? row.id])))
  ])
}

import { isThreadSession, sessionListOwnerKey, sessionListRowKey } from '@/lib/session-list-membership'
import type { SessionInfo } from '@/types/hermes'

import { hermesApiAs, type ResolvedOwner } from './client'

function parseThreadMembership(response: unknown, profile: null | string, requested: string[]): string[] | null {
  if (!response || typeof response !== 'object' || !('profile' in response) || response.profile !== profile ||
      !('thread_session_ids' in response) || !Array.isArray(response.thread_session_ids)) {
    return null
  }

  const values: unknown[] = response.thread_session_ids
  const ids: string[] = []

  for (const value of values) {
    if (typeof value !== 'string' || !requested.includes(value)) { return null }
    ids.push(value)
  }

  return ids
}

/** A pre-upgrade cache has no provenance. Probe only retained rows, in bounded
 *  exact-owner batches; a legacy detail-shaped 200 is not membership evidence. */
export async function readThreadListExclusions(
  rows: SessionInfo[],
  ambientConnection: null | string,
  owns: () => boolean
): Promise<Set<string>> {
  const excluded = new Set<string>()
  const groups = new Map<string, { owner: ResolvedOwner; rows: Map<string, SessionInfo> }>()

  for (const row of rows) {
    if (isThreadSession(row)) {
      excluded.add(sessionListRowKey(row, ambientConnection))

      continue
    }

    if (row.thread_parent_id === null || !row.id || row.id.length > 256) {
      continue
    }

    const key = sessionListOwnerKey(row, ambientConnection)
    let group = groups.get(key)

    if (!group) {
      group = { owner: { connectionId: row.connection_id ?? ambientConnection, profile: row.profile || 'default' }, rows: new Map() }
      groups.set(key, group)
    }

    group.rows.set(row.id, row)
  }

  for (const { owner, rows: groupRows } of groups.values()) {
    const ids = [...groupRows.keys()]

    for (let offset = 0; offset < ids.length; offset += 100) {
      if (!owns()) { return new Set() }
      const requested = ids.slice(offset, offset + 100)
      const query = new URLSearchParams({ profile: owner.profile || 'default', ids: JSON.stringify(requested) })
      let response: unknown

      try {
        response = await hermesApiAs<unknown>(owner, {
          method: 'GET', path: `/api/sessions/list-membership?${query}`,
          // This is background cache reconciliation, not a foreground selector.
          priority: undefined
        })
      } catch {
        // Old servers and failed reads have no authoritative exclusion opinion.
        continue
      }

      if (!owns()) { return new Set() }
      const threadIds = parseThreadMembership(response, owner.profile, requested)

      for (const id of threadIds ?? []) {
        const row = groupRows.get(id)

        if (row) { excluded.add(sessionListRowKey(row, ambientConnection)) }
      }
    }
  }

  return excluded
}

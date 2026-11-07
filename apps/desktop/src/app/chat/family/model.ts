import { isRecord } from '@hermes/shared'

import type { FamilyNode, FamilySessionResult } from '../../../../../shared/src/gateway-contract.generated'

export type FamilyDisplayNode = Pick<FamilyNode,
  'node_id' | 'parent_node_id' | 'profile' | 'session_id' | 'title' | 'full_title' | 'kind' |
  'status' | 'activity_text' | 'actions' | 'needs_you'>

export interface FamilyView extends Pick<FamilySessionResult, 'complete' | 'unavailable_profiles'> {
  nodes: FamilyDisplayNode[]
  root: FamilyDisplayNode | null
  truncated: boolean
}

const nullableString = (value: unknown): value is string | null => value === null || typeof value === 'string'
const stringList = (value: unknown): value is string[] => Array.isArray(value) && value.every(item => typeof item === 'string')
const statuses: readonly FamilyNode['status'][] = ['working', 'needs_you', 'failed', 'done', 'paused_by_restart', 'stopped', 'quiet', 'unknown']
const actions: readonly FamilyNode['actions'][number][] = ['open', 'answer', 'resume', 'stop']

function isNode(value: unknown): value is FamilyDisplayNode {
  if (!isRecord(value)) { return false }
  const needs = value.needs_you

  return typeof value.node_id === 'string' && nullableString(value.parent_node_id) &&
    typeof value.profile === 'string' && Boolean(value.profile) && nullableString(value.session_id) &&
    nullableString(value.title) && nullableString(value.full_title) && typeof value.kind === 'string' &&
    nullableString(value.activity_text) && typeof value.status === 'string' && statuses.some(status => status === value.status) &&
    stringList(value.actions) && value.actions.every(action => actions.some(known => known === action)) &&
    (needs === null || (isRecord(needs) && (needs.kind === 'clarify' || needs.kind === 'approval') &&
      typeof needs.session_id === 'string' && typeof needs.profile === 'string'))
}

/** Parse only the generated contract fields this surface consumes. Unknown protocol versions fail closed. */
export function parseFamily(value: unknown, overview: boolean): FamilyView {
  if (!isRecord(value) || value.protocol_version !== 1 || typeof value.complete !== 'boolean' ||
    !stringList(value.unavailable_profiles)) { throw new Error('Invalid Family response') }

  if (overview) {
    if (!Array.isArray(value.roots) || !value.roots.every(row => isRecord(row) && isNode(row.root)) ||
      !Array.isArray(value.earlier) || !value.earlier.every(row => isRecord(row) && isNode(row.root))) {
      throw new Error('Invalid Family overview')
    }

    const unique = new Map<string, FamilyDisplayNode>()

    for (const row of [...value.roots, ...value.earlier]) {
      if (isRecord(row) && isNode(row.root)) { unique.set(`${row.root.profile}:${row.root.node_id}`, row.root) }
    }

    return { root: null, nodes: [...unique.values()], complete: value.complete, unavailable_profiles: value.unavailable_profiles, truncated: false }
  }

  if (!isNode(value.root) || !Array.isArray(value.nodes) || !value.nodes.every(isNode) || typeof value.truncated !== 'boolean') {
    throw new Error('Invalid Family tree')
  }

  return { root: value.root, nodes: value.nodes, complete: value.complete, unavailable_profiles: value.unavailable_profiles, truncated: value.truncated }
}

/** Stable flat tree: retain all nodes, even when a parent is outside the window or malformed. */
export function familyRows(nodes: FamilyDisplayNode[]): { node: FamilyDisplayNode; depth: number }[] {
  const byId = new Map(nodes.map(node => [node.node_id, node]))

  return nodes.map(node => {
    const visited = new Set([node.node_id])
    let parent = node.parent_node_id
    let depth = 0

    while (parent && byId.has(parent) && !visited.has(parent)) {
      visited.add(parent)
      depth++
      parent = byId.get(parent)?.parent_node_id ?? null
    }

    return { node, depth: Math.min(depth, 6) }
  })
}

// Managed immutable-runtime source status (CloudSeed managed-upstream-status overlay),
// split from hermes.ts to keep that file under its line budget. Re-exported from './hermes'.

export type BackendManagedSourceAvailability = 'invalid' | 'missing' | 'ready' | 'stale' | 'unreadable'

export interface BackendManagedUpdateRefreshRequest {
  requested: boolean
  error: string | null
}

export interface BackendManagedSourceUpdate {
  schema_version: string
  count_basis?: 'recorded_official_base' | 'running_source' | 'unavailable_non_ancestral'
  availability: BackendManagedSourceAvailability
  stale: boolean
  status_error: string | null
  running_release?: string
  release_id?: string
  hermes_version?: string
  running_source?: string
  running_upstream_base?: string
  tracked_upstream?: string
  upstream_head?: string
  commits_behind?: number | null
  running_source_is_ancestor_of_upstream?: boolean | null
  local_patch_count?: number
  overlay_count?: number
  overlay_ids?: string[]
  carried_commit_count?: number
  install_mode?: string
  last_fetched_at?: string
  generated_at?: string
  age_seconds?: number
  candidate_status?: 'blocked' | 'building' | 'current' | 'not_built' | 'passed' | 'ready'
  candidate_target_revision?: string | null
  candidate_target_is_ancestor_of_upstream?: boolean | null
  running_upstream_base_is_ancestor_of_candidate_target?: boolean | null
  candidate_target_commits_behind?: number | null
  blockers?: string[]
  next_action?: string
  source_worktree_clean?: boolean
  source_refs_remotely_reachable?: boolean
  can_build_candidate: boolean
  candidate_request_available: boolean
  refresh_request_available: boolean
  refresh_request: BackendManagedUpdateRefreshRequest | null
}

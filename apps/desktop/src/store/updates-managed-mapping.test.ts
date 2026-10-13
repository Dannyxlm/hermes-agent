import { describe, expect, it } from 'vitest'

import { mapBackendCheck } from './updates'

describe('managed backend mapping', () => {
  it('maps managed immutable source status without hiding it as unsupported', () => {
    const result = mapBackendCheck({
      install_method: 'managed-runtime',
      current_version: '0.19.0',
      rollback_to: 'v0.18.0',
      behind: 4,
      update_available: true,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: '4 upstream commits are waiting for an immutable candidate.',
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        availability: 'ready',
        stale: false,
        status_error: null,
        count_basis: 'running_source',
        running_release: 'ava-converge-p1-f22a217b8dab',
        running_source: 'c'.repeat(40),
        running_upstream_base: 'a'.repeat(40),
        tracked_upstream: 'NousResearch/main',
        upstream_head: 'b'.repeat(40),
        commits_behind: 4,
        local_patch_count: 2,
        last_fetched_at: '2026-07-27T18:00:00+00:00',
        generated_at: '2026-07-27T18:00:00+00:00',
        candidate_status: 'not_built',
        blockers: [],
        next_action: 'Build an immutable candidate.',
        source_worktree_clean: true,
        source_refs_remotely_reachable: true,
        can_build_candidate: true,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: null
      }
    })

    expect(result?.rollback).toBe(false)
    expect(result?.sourceVersion).toBeUndefined()
    expect(result?.supported).toBe(true)
    expect(result?.behind).toBe(4)
    expect(result?.currentVersion).toBe('0.19.0')
    expect(result?.targetSha).toBe('b'.repeat(40))
    expect(result?.managedSource?.countBasis).toBe('running_source')
    expect(result?.managedSource?.runningSource).toBe('c'.repeat(40))
    expect(result?.managedSource?.candidateStatus).toBe('not_built')
    expect(result?.managedSource?.canBuildCandidate).toBe(true)
  })
})

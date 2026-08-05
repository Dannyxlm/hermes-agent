import assert from 'node:assert/strict'
import { mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { test } from 'vitest'

import {
  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  FALLBACK_REPOSITORY,
  branchFromCI,
  fromCI,
  fromFallback,
  fromLocalGit,
  isFallbackCommit,
  normalizeRepository,
  releaseProvenance,
  resolveStamp
} from './write-build-stamp.mjs'

test('fromCI reads exact publication provenance', () => {
  assert.deepEqual(
    fromCI({
      GITHUB_SHA: 'a'.repeat(40),
      GITHUB_REF_NAME: 'release',
      GITHUB_REF_TYPE: 'branch',
      GITHUB_REPOSITORY: 'Dannyxlm/hermes-agent'
    }),
    {
      commit: 'a'.repeat(40),
      branch: 'release',
      repository: 'Dannyxlm/hermes-agent',
      dirty: false,
      source: 'ci'
    }
  )
  assert.equal(fromCI({}), null)
})

test('installer-seeded provenance preserves the declared repository and dirty tree state', () => {
  assert.deepEqual(
    fromCI({
      GITHUB_SHA: 'a'.repeat(40),
      GITHUB_REF_NAME: 'release',
      HERMES_DESKTOP_UPDATE_DIRTY: 'true',
      HERMES_DESKTOP_UPDATE_REPOSITORY: 'Dannyxlm/hermes-agent'
    }),
    {
      commit: 'a'.repeat(40),
      branch: 'release',
      repository: 'Dannyxlm/hermes-agent',
      dirty: true,
      source: 'ci'
    }
  )
})

test('CI provenance never stamps a synthetic pull-request merge branch or a release tag', () => {
  assert.equal(
    branchFromCI({ GITHUB_REF_NAME: '123/merge', GITHUB_HEAD_REF: 'fix/desktop', GITHUB_REF_TYPE: 'branch' }),
    'fix/desktop'
  )
  assert.equal(branchFromCI({ GITHUB_REF_NAME: 'v0.18.0', GITHUB_REF_TYPE: 'tag' }), FALLBACK_BRANCH)
  assert.equal(branchFromCI({ HERMES_DESKTOP_UPDATE_BRANCH: 'main', GITHUB_REF_NAME: '123/merge' }), 'main')
  assert.equal(normalizeRepository('git@github.com:Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
  assert.equal(normalizeRepository('https://github.com/Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
})

test('fromLocalGit returns null when git rev-parse fails', () => {
  const stamp = fromLocalGit('/tmp/not-a-repo', () => null)
  assert.equal(stamp, null)
})

test('fromLocalGit reads HEAD + branch + dirty status', () => {
  const calls = []
  const execFn = (cmd) => {
    calls.push(cmd)
    if (cmd === 'git rev-parse HEAD') return 'b'.repeat(40)
    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git remote get-url origin') return 'git@github.com:Dannyxlm/hermes-agent.git'
    if (cmd === 'git status --porcelain -uno') return ' M apps/desktop/package.json'
    return null
  }
  assert.deepEqual(fromLocalGit('/repo', execFn), {
    commit: 'b'.repeat(40),
    branch: 'main',
    repository: 'Dannyxlm/hermes-agent',
    dirty: true,
    source: 'local'
  })
  assert.ok(calls.includes('git rev-parse HEAD'))
})

test('fromFallback uses the all-zero placeholder commit', () => {
  assert.deepEqual(fromFallback(), {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
  assert.equal(isFallbackCommit(FALLBACK_COMMIT), true)
  assert.equal(isFallbackCommit('a'.repeat(40)), false)
})

test('resolveStamp prefers CI over local git over fallback', () => {
  const ci = resolveStamp({
    env: { GITHUB_SHA: 'c'.repeat(40), GITHUB_REF_NAME: 'main' },
    execFn: () => 'should-not-run'
  })
  assert.equal(ci.source, 'ci')
  assert.equal(ci.commit, 'c'.repeat(40))

  const local = resolveStamp({
    env: {},
    execFn: (cmd) => {
      if (cmd === 'git rev-parse HEAD') return 'd'.repeat(40)
      if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
      if (cmd === 'git status --porcelain -uno') return ''
      return null
    }
  })
  assert.equal(local.source, 'local')
  assert.equal(local.commit, 'd'.repeat(40))
  assert.equal(local.dirty, false)
})

test('resolveStamp falls back when neither CI nor git is available', () => {
  const stamp = resolveStamp({ env: {}, execFn: () => null })
  assert.deepEqual(stamp, {
    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,
    source: 'fallback'
  })
})

test('release provenance binds the frozen upstream and semantic overlay ledger', () => {
  const root = mkdtempSync(join(tmpdir(), 'hermes-overlay-stamp-'))
  const manifest = join(root, 'overlays.json')
  const publication = join(root, 'publication.json')
  writeFileSync(
    manifest,
    JSON.stringify({
      schema_version: 'cloudseed-hermes-overlays.v1',
      overlays: [{ id: 'managed-desktop-publication' }, { id: 'managed-upstream-status' }]
    })
  )
  writeFileSync(
    publication,
    JSON.stringify({
      schema_version: 'cloudseed-hermes-publication.v1',
      release_id: 'hermes-20260804-aec3318',
      official_repository: 'NousResearch/hermes-agent',
      official_branch: 'main',
      official_revision: 'a'.repeat(40),
      official_release_tag: 'v2026.8.3',
      integration_repository: 'Dannyxlm/hermes-agent',
      integration_branch: 'main',
      desktop_install_mode: 'managed-publication',
      desktop_self_update_allowed: true,
      overlay_transition: {
        added: ['managed-desktop-publication'],
        retained: ['managed-upstream-status'],
        retired: []
      }
    })
  )

  const provenance = releaseProvenance(
    {},
    manifest,
    publication
  )

  assert.equal(provenance.releaseId, 'hermes-20260804-aec3318')
  assert.equal(provenance.officialUpstreamCommit, 'a'.repeat(40))
  assert.equal(provenance.officialReleaseTag, 'v2026.8.3')
  assert.deepEqual(provenance.overlayIds, ['managed-desktop-publication', 'managed-upstream-status'])
  assert.match(provenance.overlayManifestSha256, /^[0-9a-f]{64}$/)
  assert.equal(provenance.installMode, 'managed-publication')
  assert.equal(provenance.selfUpdateAllowed, true)
})

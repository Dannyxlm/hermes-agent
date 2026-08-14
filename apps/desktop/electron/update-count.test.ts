import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import type { OfficialUpstreamTracking } from './update-count'
import {
  inspectOfficialUpstream,
  INSTALLED_BUILD_REF,
  normalizeGitHubRepository,
  OFFICIAL_UPSTREAM_REF,
  OFFICIAL_UPSTREAM_URL,
  probeCheckoutIdentity,
  resolveBehindCount,
  resolveIdentityHistorySource,
  resolveInstalledIdentity,
  resolveLegacyUpdateSafety,
  resolveManagedPublicationBranch,
  resolveManagedPublicationSafety,
  shouldCountCommits
} from './update-count'

test('managed packaged updates stay pinned to their stamped branch', () => {
  assert.equal(resolveManagedPublicationBranch(true, { branch: 'main' }, 'feature/override'), 'main')
  assert.equal(resolveManagedPublicationBranch(false, { branch: 'main' }, 'feature/dev'), 'feature/dev')
})

// FAIL-BEFORE: pre-fix the function did `Number.parseInt(countStr) || 0`
// unconditionally, so a shallow checkout with no merge-base surfaced the bogus
// rev-list count (e.g. 12104). This asserts the new shallow/no-merge-base branch.
test('shallow checkout with no merge-base does NOT trust the bogus rev-list count', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '12104',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: true,
      hasMergeBase: false
    }),
    1
  )
})

test('shallow checkout with no merge-base but identical SHA reports up-to-date', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '12104',
      currentSha: 'abc',
      targetSha: 'abc',
      isShallow: true,
      hasMergeBase: false
    }),
    0
  )
})

test('legacy packaged identity reads history locally instead of inferring a mutable remote', () => {
  const identity = resolveInstalledIdentity({
    installStamp: {
      branch: 'main',
      commit: '1234567890abcdef1234567890abcdef12345678'
    },
    packaged: true
  })

  assert.equal(resolveIdentityHistorySource(identity, '/local/hermes-checkout'), '/local/hermes-checkout')
})

test('repository-stamped packaged identity reads history from its declared producer', () => {
  const identity = resolveInstalledIdentity({
    installStamp: {
      branch: 'main',
      commit: '1234567890abcdef1234567890abcdef12345678',
      repository: 'Dannyxlm/hermes-agent'
    },
    packaged: true
  })

  assert.equal(
    resolveIdentityHistorySource(identity, '/local/hermes-checkout'),
    'https://github.com/Dannyxlm/hermes-agent.git'
  )
})

test('shallow checkout WITH a merge-base keeps the exact count (reliable)', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '3',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: true,
      hasMergeBase: true
    }),
    3
  )
})

test('full (non-shallow) clone keeps the exact count path unchanged', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '7',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: false,
      hasMergeBase: true
    }),
    7
  )
})

test('up-to-date full clone reports 0', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '0',
      currentSha: 'x',
      targetSha: 'x',
      isShallow: false,
      hasMergeBase: true
    }),
    0
  )
})

test('non-numeric count falls back to 0 (defensive, unchanged behaviour)', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: false,
      hasMergeBase: true
    }),
    0
  )
})

// shouldCountCommits gates the expensive `rev-list --count` in checkUpdates().
// FAIL-BEFORE: in the shallow + no-merge-base case the caller ran rev-list
// unconditionally and discarded the bogus result; this predicate lets the
// caller SKIP the whole-ancestry enumeration in exactly that case (#51922).
test('shallow checkout with no merge-base SKIPS the rev-list count', () => {
  assert.equal(shouldCountCommits({ isShallow: true, hasMergeBase: false }), false)
})

test('shallow checkout WITH a merge-base still runs the count', () => {
  assert.equal(shouldCountCommits({ isShallow: true, hasMergeBase: true }), true)
})

test('full (non-shallow) clone always runs the count', () => {
  assert.equal(shouldCountCommits({ isShallow: false, hasMergeBase: true }), true)
  assert.equal(shouldCountCommits({ isShallow: false, hasMergeBase: false }), true)
})

// The skip path produces an empty countStr; resolveBehindCount must NOT trust
// it and must fall through to the SHA compare (mirrors the live call site).
test('skipped-count path resolves via SHA compare, never via empty countStr', () => {
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'aaa',
      targetSha: 'bbb',
      isShallow: true,
      hasMergeBase: false
    }),
    1
  )
  assert.equal(
    resolveBehindCount({
      countStr: '',
      currentSha: 'same',
      targetSha: 'same',
      isShallow: true,
      hasMergeBase: false
    }),
    0
  )
})

test('packaged install stamp identity wins over a newer mutable checkout HEAD', () => {
  const stamped = 'a'.repeat(40)
  const mutableHead = 'b'.repeat(40)

  assert.deepEqual(
    resolveInstalledIdentity({
      checkoutHead: mutableHead,
      installStamp: {
        commit: stamped,
        dirty: false,
        repository: 'Dannyxlm/hermes-agent'
      },
      packaged: true
    }),
    {
      branch: null,
      dirty: false,
      error: null,
      repository: 'Dannyxlm/hermes-agent',
      sha: stamped,
      source: 'install-stamp'
    }
  )
})

test('packaged app without a valid stamp refuses to masquerade as mutable checkout HEAD', () => {
  assert.deepEqual(
    resolveInstalledIdentity({
      checkoutHead: 'b'.repeat(40),
      installStamp: null,
      packaged: true
    }),
    {
      branch: null,
      dirty: false,
      error: 'install-stamp-missing',
      repository: null,
      sha: null,
      source: null
    }
  )
})

test('dev runs prefer the current checkout HEAD over a stale local build stamp', () => {
  const checkoutHead = 'c'.repeat(40)

  assert.deepEqual(
    resolveInstalledIdentity({
      checkoutHead,
      installStamp: {
        branch: 'old-build',
        commit: 'a'.repeat(40),
        repository: 'Dannyxlm/hermes-agent'
      },
      packaged: false
    }),
    {
      branch: null,
      dirty: false,
      error: null,
      repository: null,
      sha: checkoutHead,
      source: 'checkout-head'
    }
  )
})

test('mutable checkout identity preserves tracked local changes for update safety', () => {
  const identity = resolveInstalledIdentity({
    checkoutBranch: 'main',
    checkoutDirty: true,
    checkoutHead: 'c'.repeat(40),
    checkoutRepository: 'Dannyxlm/hermes-agent',
    packaged: false
  })

  assert.equal(identity.dirty, true)
  assert.equal(identity.sha, 'c'.repeat(40))
  assert.deepEqual(
    resolveLegacyUpdateSafety(
      true,
      trackingStatus({
        identityDirty: identity.dirty,
        identitySource: identity.source,
        installedRepository: identity.repository,
        installedSha: identity.sha
      }),
      'update-target'
    ),
    {
      allowed: false,
      message: 'Automatic updates are disabled because the mutable Hermes checkout has tracked local changes.',
      reason: 'identity-dirty'
    }
  )
})

test('dirty packaged build stamps fail closed instead of claiming an exact identity', () => {
  assert.deepEqual(
    resolveInstalledIdentity({
      checkoutHead: 'b'.repeat(40),
      installStamp: {
        branch: 'main',
        commit: 'a'.repeat(40),
        dirty: true,
        repository: 'Dannyxlm/hermes-agent'
      },
      packaged: true
    }),
    {
      branch: 'main',
      dirty: true,
      error: 'dirty-install-stamp',
      repository: 'Dannyxlm/hermes-agent',
      sha: null,
      source: 'install-stamp'
    }
  )
})

test('GitHub checkout remotes recover repository provenance for legacy stamps', () => {
  assert.equal(normalizeGitHubRepository('git@github.com:Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
  assert.equal(normalizeGitHubRepository('ssh://git@github.com/Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
  assert.equal(normalizeGitHubRepository('https://github.com/Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
  assert.equal(normalizeGitHubRepository('https://example.com/Dannyxlm/hermes-agent.git'), null)
})

test('checkout identity probes bound every Git read and fail closed on timeout', async () => {
  const calls: Array<{ args: string[]; timeoutMs: number }> = []

  const result = await probeCheckoutIdentity({
    runGit: async (args, options) => {
      calls.push({ args, timeoutMs: options.timeoutMs })

      if (args[0] === 'status') {
        return { code: 124, stderr: 'git command timed out.', stdout: '' }
      }

      if (args[0] === 'remote') {
        return { code: 0, stderr: '', stdout: 'git@github.com:Dannyxlm/hermes-agent.git\n' }
      }

      if (args.includes('--abbrev-ref')) {
        return { code: 0, stderr: '', stdout: 'main\n' }
      }

      return { code: 0, stderr: '', stdout: `${'a'.repeat(40)}\n` }
    },
    timeoutMs: 321
  })

  assert.equal(calls.length, 4)
  assert.ok(calls.every(call => call.timeoutMs === 321))
  assert.deepEqual(result, {
    checkoutBranch: 'main',
    checkoutDirty: true,
    checkoutHead: 'a'.repeat(40),
    checkoutRepository: 'Dannyxlm/hermes-agent'
  })
})

function runGit(cwd: string, args: string[]) {
  const result = spawnSync('git', args, {
    cwd,
    encoding: 'utf8'
  })

  return {
    code: result.status ?? 1,
    stderr: result.stderr || '',
    stdout: result.stdout || ''
  }
}

function git(cwd: string, args: string[]): string {
  const result = runGit(cwd, args)

  assert.equal(result.code, 0, `git ${args.join(' ')} failed: ${result.stderr}`)

  return result.stdout.trim()
}

function commitFile(cwd: string, name: string, contents: string, message: string): string {
  fs.writeFileSync(path.join(cwd, name), contents)
  git(cwd, ['add', name])
  git(cwd, ['commit', '-m', message])

  return git(cwd, ['rev-parse', 'HEAD'])
}

test('dedicated official ref bypasses a stale remote.origin.fetch and cannot report a false zero', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upstream-count-'))
  const official = path.join(root, 'official.git')
  const publisher = path.join(root, 'publisher')
  const installed = path.join(root, 'installed')
  const tracking = path.join(root, 'tracking.git')
  const officialTracking = path.join(root, 'official-tracking.git')

  try {
    fs.mkdirSync(publisher)
    git(root, ['init', '--bare', official])
    git(root, ['init', '--bare', tracking])
    git(root, ['init', '--bare', officialTracking])
    git(publisher, ['init', '-b', 'main'])
    git(publisher, ['config', 'user.email', 'desktop-test@example.invalid'])
    git(publisher, ['config', 'user.name', 'Desktop Test'])

    const installedCommit = commitFile(publisher, 'history.txt', 'base\n', 'base')
    git(publisher, ['remote', 'add', 'origin', official])
    git(publisher, ['push', '-u', 'origin', 'main'])
    git(root, ['clone', '--branch', 'main', official, installed])

    commitFile(publisher, 'history.txt', 'base\none\n', 'upstream one')
    const officialHead = commitFile(publisher, 'history.txt', 'base\none\ntwo\n', 'upstream two')
    git(publisher, ['push', 'origin', 'main'])

    // Reproduce the field failure: origin only fetches an unrelated branch, so
    // a plain `git fetch origin main` leaves refs/remotes/origin/main at base.
    git(installed, ['config', '--unset-all', 'remote.origin.fetch'])
    git(installed, ['config', '--add', 'remote.origin.fetch', '+refs/heads/unrelated:refs/remotes/origin/unrelated'])
    assert.equal(git(installed, ['rev-parse', 'origin/main']), installedCommit)

    const status = await inspectOfficialUpstream({
      identity: resolveInstalledIdentity({
        checkoutHead: officialHead,
        installStamp: {
          branch: 'main',
          commit: installedCommit
        },
        packaged: true
      }),
      // Old schema-v1 stamps did not include repository. main.ts falls back to
      // the existing source checkout as a read-only identity-history source.
      identityRepositoryUrl: installed,
      officialCacheUrl: officialTracking,
      repository: 'NousResearch/hermes-agent',
      repositoryUrl: official,
      runGit: args => Promise.resolve(runGit(tracking, args)),
      runOfficialGit: args => Promise.resolve(runGit(officialTracking, args))
    })

    assert.equal(status.state, 'ready')
    assert.equal(status.behind, 2)
    assert.equal(status.ahead, 0)
    assert.equal(status.installedRepository, null)
    assert.equal(status.installedSha, installedCommit)
    assert.equal(status.targetSha, officialHead)
    assert.equal(git(installed, ['rev-parse', 'origin/main']), installedCommit)
    assert.equal(git(tracking, ['rev-parse', OFFICIAL_UPSTREAM_REF]), officialHead)
    assert.equal(runGit(tracking, ['cat-file', '-e', `${installedCommit}^{commit}`]).code, 0)
    assert.notEqual(runGit(installed, ['rev-parse', '--verify', OFFICIAL_UPSTREAM_REF]).code, 0)
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

test('isolated tracking cache recovers exact fork distance from a real depth-1 install', async () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-upstream-shallow-'))
  const official = path.join(root, 'official.git')
  const fork = path.join(root, 'fork.git')
  const seed = path.join(root, 'seed')
  const upstream = path.join(root, 'upstream')
  const installed = path.join(root, 'installed')
  const tracking = path.join(root, 'tracking.git')
  const oldStampTracking = path.join(root, 'old-stamp-tracking.git')
  const officialTracking = path.join(root, 'official-tracking.git')

  try {
    git(root, ['init', '--bare', official])
    git(root, ['init', '--bare', fork])
    fs.mkdirSync(seed)
    git(seed, ['init', '-b', 'main'])
    git(seed, ['config', 'user.email', 'desktop-test@example.invalid'])
    git(seed, ['config', 'user.name', 'Desktop Test'])

    commitFile(seed, 'history.txt', 'base\n', 'base')
    git(seed, ['remote', 'add', 'official', official])
    git(seed, ['remote', 'add', 'fork', fork])
    git(seed, ['push', 'official', 'main'])
    git(seed, ['push', 'fork', 'main'])

    const installedCommit = commitFile(seed, 'fork.txt', 'cloudseed patch\n', 'fork-only patch')
    git(seed, ['push', 'fork', 'main'])

    git(root, ['clone', '--branch', 'main', official, upstream])
    git(upstream, ['config', 'user.email', 'desktop-test@example.invalid'])
    git(upstream, ['config', 'user.name', 'Desktop Test'])
    commitFile(upstream, 'history.txt', 'base\none\n', 'upstream one')
    const officialHead = commitFile(upstream, 'history.txt', 'base\none\ntwo\n', 'upstream two')
    git(upstream, ['push', 'origin', 'main'])

    // file:// is intentional: Git ignores --depth for a plain local-path clone.
    git(root, ['clone', '--depth', '1', '--branch', 'main', `file://${fork}`, installed])
    git(root, ['init', '--bare', tracking])
    git(root, ['init', '--bare', oldStampTracking])
    git(root, ['init', '--bare', officialTracking])

    assert.equal(git(installed, ['rev-parse', '--is-shallow-repository']), 'true')
    assert.equal(git(installed, ['rev-parse', 'HEAD']), installedCommit)
    const beforeConfig = git(installed, ['config', '--local', '--list'])
    const beforeStatus = git(installed, ['status', '--porcelain=v1'])

    const status = await inspectOfficialUpstream({
      identity: resolveInstalledIdentity({
        checkoutHead: installedCommit,
        installStamp: {
          branch: 'main',
          commit: installedCommit,
          repository: 'Dannyxlm/hermes-agent'
        },
        packaged: true
      }),
      identityRepositoryUrl: fork,
      officialCacheUrl: officialTracking,
      repositoryUrl: official,
      runGit: args => Promise.resolve(runGit(tracking, args)),
      runOfficialGit: args => Promise.resolve(runGit(officialTracking, args))
    })

    assert.equal(status.state, 'ready')
    assert.equal(status.behind, 2)
    assert.equal(status.ahead, 1)
    assert.equal(status.installedSha, installedCommit)
    assert.equal(status.targetSha, officialHead)
    assert.equal(git(installed, ['config', '--local', '--list']), beforeConfig)
    assert.equal(git(installed, ['status', '--porcelain=v1']), beforeStatus)
    assert.equal(git(installed, ['rev-parse', 'HEAD']), installedCommit)
    assert.notEqual(runGit(installed, ['rev-parse', '--verify', OFFICIAL_UPSTREAM_REF]).code, 0)
    assert.notEqual(runGit(officialTracking, ['cat-file', '-e', `${installedCommit}^{commit}`]).code, 0)

    // Legacy schema-v1 stamps had no repository field. A full existing
    // checkout remains a valid, read-only history source and must still expose
    // fork divergence exactly before the legacy updater can be considered.
    const oldStampStatus = await inspectOfficialUpstream({
      identity: resolveInstalledIdentity({
        checkoutHead: officialHead,
        installStamp: { branch: 'main', commit: installedCommit },
        packaged: true
      }),
      identityRepositoryUrl: seed,
      officialCacheUrl: officialTracking,
      repositoryUrl: official,
      runGit: args => Promise.resolve(runGit(oldStampTracking, args)),
      runOfficialGit: args => Promise.resolve(runGit(officialTracking, args))
    })

    assert.equal(oldStampStatus.state, 'ready')
    assert.equal(oldStampStatus.installedRepository, null)
    assert.equal(oldStampStatus.behind, 2)
    assert.equal(oldStampStatus.ahead, 1)
    assert.equal(resolveLegacyUpdateSafety(true, oldStampStatus).reason, 'fork-divergent')
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

test('fetch failure uses an existing dedicated ref only as explicitly stale data', async () => {
  const identity = resolveInstalledIdentity({
    checkoutHead: null,
    installStamp: { commit: 'a'.repeat(40), repository: 'Dannyxlm/hermes-agent' },
    packaged: true
  })

  const calls: string[][] = []

  const status = await inspectOfficialUpstream({
    identity,
    now: () => 1234,
    runGit: async args => {
      calls.push(args)

      if (args[0] === 'fetch') {
        return { code: 1, stderr: 'offline\n', stdout: '' }
      }

      if (args[0] === 'rev-parse') {
        return { code: 0, stderr: '', stdout: `${'b'.repeat(40)}\n` }
      }

      if (args[0] === 'cat-file' || args[0] === 'merge-base') {
        return { code: 0, stderr: '', stdout: `${'c'.repeat(40)}\n` }
      }

      return { code: 0, stderr: '', stdout: '5\t2\n' }
    }
  })

  assert.equal(status.state, 'stale')
  assert.equal(status.error, 'fetch-failed')
  assert.equal(status.message, 'offline')
  assert.equal(status.behind, 5)
  assert.equal(status.ahead, 2)
  assert.equal(status.fetchedAt, null)
  assert.ok(calls.some(args => args.at(-1) === `+refs/heads/main:${OFFICIAL_UPSTREAM_REF}`))
  assert.ok(calls.every(args => !args.some(arg => arg === 'origin/main')))
})

test('mutable fork history hydration never sends comparison-cache fetches to official GitHub', async () => {
  const comparisonCalls: string[][] = []
  const officialCalls: string[][] = []
  let mergeBaseCalls = 0

  const identity = resolveInstalledIdentity({
    checkoutBranch: 'local-fork',
    checkoutHead: 'a'.repeat(40),
    checkoutRepository: 'NousResearch/hermes-agent',
    packaged: false
  })

  const status = await inspectOfficialUpstream({
    identity,
    identityRepositoryUrl: '/local/hermes-checkout',
    officialCacheUrl: '/cache/official-upstream.git',
    repositoryUrl: OFFICIAL_UPSTREAM_URL,
    runGit: async args => {
      comparisonCalls.push(args)

      if (args[0] === 'cat-file') {
        return { code: 0, stderr: '', stdout: '' }
      }

      if (args[0] === 'rev-parse') {
        return { code: 0, stderr: '', stdout: `${'b'.repeat(40)}\n` }
      }

      if (args[0] === 'merge-base') {
        mergeBaseCalls += 1

        return mergeBaseCalls === 1
          ? { code: 1, stderr: 'history missing', stdout: '' }
          : { code: 0, stderr: '', stdout: `${'c'.repeat(40)}\n` }
      }

      if (args[0] === 'rev-list') {
        return { code: 0, stderr: '', stdout: '5\t2\n' }
      }

      return { code: 0, stderr: '', stdout: '' }
    },
    runOfficialGit: async args => {
      officialCalls.push(args)

      return { code: 0, stderr: '', stdout: '' }
    }
  })

  assert.equal(status.state, 'ready')
  assert.equal(status.behind, 5)
  assert.equal(status.ahead, 2)
  assert.ok(comparisonCalls.some(args => args.includes('/cache/official-upstream.git')))
  assert.ok(comparisonCalls.some(args => args.includes('/local/hermes-checkout')))
  assert.ok(comparisonCalls.every(args => !args.includes('https://github.com/NousResearch/hermes-agent.git')))
  assert.ok(officialCalls.every(args => !args.includes('/local/hermes-checkout')))
})

test('official-stamped identity hydration stays in the official-only cache', async () => {
  const comparisonCalls: string[][] = []
  const officialCalls: string[][] = []
  let catFileCalls = 0
  const installedSha = 'a'.repeat(40)
  const targetSha = 'b'.repeat(40)

  const identity = resolveInstalledIdentity({
    installStamp: {
      branch: 'release',
      commit: installedSha,
      repository: 'NousResearch/hermes-agent'
    },
    packaged: true
  })

  const status = await inspectOfficialUpstream({
    identity,
    identityRepositoryUrl: OFFICIAL_UPSTREAM_URL,
    officialCacheUrl: '/cache/official-upstream.git',
    runGit: async args => {
      comparisonCalls.push(args)

      if (args[0] === 'cat-file') {
        catFileCalls += 1

        return catFileCalls === 1
          ? { code: 1, stderr: 'missing from fork-polluted comparison cache', stdout: '' }
          : { code: 0, stderr: '', stdout: '' }
      }

      if (args[0] === 'rev-parse') {
        return { code: 0, stderr: '', stdout: `${targetSha}\n` }
      }

      if (args[0] === 'merge-base') {
        return { code: 0, stderr: '', stdout: `${installedSha}\n` }
      }

      if (args[0] === 'rev-list') {
        return { code: 0, stderr: '', stdout: '5\t0\n' }
      }

      return { code: 0, stderr: '', stdout: '' }
    },
    runOfficialGit: async args => {
      officialCalls.push(args)

      // Force the exact-SHA fetch to fail so the stamped branch fallback is
      // also proven to stay inside the official-only network cache.
      if (args.at(-1) === `+${installedSha}:${INSTALLED_BUILD_REF}`) {
        return { code: 1, stderr: 'exact SHA not advertised', stdout: '' }
      }

      return { code: 0, stderr: '', stdout: '' }
    }
  })

  assert.equal(status.state, 'ready')
  assert.equal(status.behind, 5)
  assert.equal(status.ahead, 0)
  assert.ok(officialCalls.some(args => args.includes(OFFICIAL_UPSTREAM_URL)))
  assert.ok(
    officialCalls.some(args => args.at(-1) === `+refs/heads/release:${INSTALLED_BUILD_REF}`),
    'stamped branch fallback must run in the official-only cache'
  )
  assert.ok(comparisonCalls.every(args => !args.includes(OFFICIAL_UPSTREAM_URL)))
  assert.ok(
    comparisonCalls.some(
      args =>
        args.includes('/cache/official-upstream.git') &&
        args.at(-1) === `+${INSTALLED_BUILD_REF}:${INSTALLED_BUILD_REF}`
    ),
    'comparison cache imports the hydrated official identity locally'
  )
})

test('fetch failure without a dedicated ref is an error with unknown distance, never zero', async () => {
  const status = await inspectOfficialUpstream({
    identity: resolveInstalledIdentity({
      checkoutHead: null,
      installStamp: { commit: 'a'.repeat(40) },
      packaged: true
    }),
    runGit: async args => {
      if (args[0] === 'cat-file') {
        return { code: 0, stderr: '', stdout: '' }
      }

      return args[0] === 'fetch'
        ? { code: 1, stderr: 'offline\n', stdout: '' }
        : { code: 1, stderr: 'missing\n', stdout: '' }
    }
  })

  assert.equal(status.state, 'error')
  assert.equal(status.error, 'fetch-failed')
  assert.equal(status.behind, null)
  assert.equal(status.ahead, null)
})

function trackingStatus(overrides: Partial<OfficialUpstreamTracking> = {}): OfficialUpstreamTracking {
  return {
    ahead: 0,
    behind: 0,
    branch: 'main',
    checkedAt: 1,
    error: null,
    fetchedAt: 1,
    identityDirty: false,
    identitySource: 'install-stamp',
    installedBranch: 'main',
    installedRepository: 'Dannyxlm/hermes-agent',
    installedSha: 'a'.repeat(40),
    message: null,
    readOnly: true,
    repository: 'NousResearch/hermes-agent',
    state: 'ready',
    targetSha: 'b'.repeat(40),
    trackingRef: OFFICIAL_UPSTREAM_REF,
    ...overrides
  }
}

test('packaged legacy updater is blocked when the build contains fork-only commits', () => {
  assert.deepEqual(resolveLegacyUpdateSafety(true, trackingStatus({ ahead: 5 })), {
    allowed: false,
    message: 'Automatic updates are disabled because this build has 5 fork-only commits.',
    reason: 'fork-divergent'
  })
})

test('packaged legacy updater fails closed when upstream ancestry is unproven', () => {
  assert.deepEqual(
    resolveLegacyUpdateSafety(
      true,
      trackingStatus({
        ahead: null,
        behind: null,
        error: 'history-unavailable',
        state: 'error'
      })
    ),
    {
      allowed: false,
      message: 'Automatic updates are disabled until the packaged app can be compared safely with official upstream.',
      reason: 'upstream-unproven'
    }
  )
})

test('packaged legacy updater is allowed only after proving there are no fork-only commits', () => {
  assert.deepEqual(resolveLegacyUpdateSafety(true, trackingStatus({ ahead: 0, behind: 12 })), {
    allowed: true,
    message: null,
    reason: null
  })
})

test('safe package provenance cannot authorize a fork-divergent mutable checkout', () => {
  assert.equal(resolveLegacyUpdateSafety(true, trackingStatus({ ahead: 0 })).allowed, true)
  assert.deepEqual(resolveLegacyUpdateSafety(true, trackingStatus({ ahead: 2 }), 'update-target'), {
    allowed: false,
    message: 'Automatic updates are disabled because the mutable Hermes checkout has 2 fork-only commits.',
    reason: 'fork-divergent'
  })
})

test('dev runs keep their existing update workflow regardless of fork distance', () => {
  assert.deepEqual(resolveLegacyUpdateSafety(false, trackingStatus({ ahead: 5 })), {
    allowed: true,
    message: null,
    reason: null
  })
})

test('managed packaged updater allows expected fork commits from the stamped publication', () => {
  const installed = trackingStatus({ ahead: 25 })
  const updateTarget = trackingStatus({
    ahead: 26,
    identitySource: 'checkout-head',
    installedSha: 'c'.repeat(40)
  })

  assert.deepEqual(resolveManagedPublicationSafety(true, installed, updateTarget), {
    allowed: true,
    message: null,
    reason: null
  })
})

test('managed packaged updater rejects a different publication checkout', () => {
  const installed = trackingStatus({ ahead: 25 })
  const updateTarget = trackingStatus({
    installedRepository: 'NousResearch/hermes-agent',
    identitySource: 'checkout-head',
    installedSha: 'c'.repeat(40)
  })

  assert.deepEqual(resolveManagedPublicationSafety(true, installed, updateTarget), {
    allowed: false,
    message:
      'Automatic updates are disabled because this app was published from Dannyxlm/hermes-agent@main, but the managed checkout follows NousResearch/hermes-agent@main.',
    reason: 'publication-mismatch'
  })
})

test('managed packaged updater rejects a dirty publication checkout', () => {
  assert.equal(
    resolveManagedPublicationSafety(true, trackingStatus(), trackingStatus({ identityDirty: true })).reason,
    'identity-dirty'
  )
})

// Whether `git rev-list HEAD..origin/<branch> --count` produces a meaningful
// number worth computing. On a SHALLOW checkout (installer clones with
// --depth 1) the local history often shares no merge-base with the freshly
// fetched origin tip, so the count enumerates the entire remote ancestry and
// returns a bogus huge number (e.g. 12104) — see #51922. resolveBehindCount
// discards that bogus count in favour of a SHA compare, so the caller should
// SKIP the expensive rev-list entirely in that case rather than run it and
// throw the result away.
function shouldCountCommits({ isShallow, hasMergeBase }) {
  return !(isShallow && !hasMergeBase)
}

// Resolve how many commits the local checkout is behind origin for the desktop
// update indicator. When the count isn't meaningful (shallow + no merge-base)
// fall back to a binary up-to-date check by SHA, exactly like the official-SSH
// path in checkUpdates() and the CLI guard in hermes_cli/banner.py. Full clones
// (developers / Docker dev images) keep the exact count path unchanged.
function resolveBehindCount({ countStr, currentSha, targetSha, isShallow, hasMergeBase }) {
  if (!shouldCountCommits({ isShallow, hasMergeBase })) {
    if (currentSha && targetSha && currentSha === targetSha) {
      return 0
    }

    return 1 // behind by an unknown amount — show a generic "update available"
  }

  return Number.parseInt(countStr, 10) || 0
}

const OFFICIAL_UPSTREAM_BRANCH = 'main'
const OFFICIAL_UPSTREAM_REPOSITORY = 'NousResearch/hermes-agent'
const OFFICIAL_UPSTREAM_URL = `https://github.com/${OFFICIAL_UPSTREAM_REPOSITORY}.git`
const OFFICIAL_UPSTREAM_REF = `refs/hermes-desktop-upstream/${OFFICIAL_UPSTREAM_BRANCH}`
const INSTALLED_BUILD_REF = 'refs/hermes-desktop-build/installed'
const COMMIT_RE = /^[0-9a-f]{7,40}$/i
const REPOSITORY_RE = /^[0-9A-Za-z][0-9A-Za-z_.-]*\/[0-9A-Za-z][0-9A-Za-z_.-]*$/

interface GitResult {
  code: number
  stderr: string
  stdout: string
}

interface InstallStampInput {
  branch?: unknown
  commit?: unknown
  dirty?: unknown
  repository?: unknown
}

interface InstalledIdentity {
  branch: string | null
  dirty: boolean
  error: 'dirty-install-stamp' | 'install-stamp-missing' | 'installed-identity-unavailable' | null
  repository: string | null
  sha: string | null
  source: 'checkout-head' | 'install-stamp' | null
}

interface ResolveInstalledIdentityOptions {
  checkoutBranch?: unknown
  checkoutDirty?: unknown
  checkoutHead?: unknown
  checkoutRepository?: unknown
  installStamp?: InstallStampInput | null
  packaged: boolean
}

interface InspectOfficialUpstreamOptions {
  branch?: string
  identity: InstalledIdentity
  identityRepositoryUrl?: string | null
  now?: () => number
  officialCacheUrl?: string | null
  repository?: string
  repositoryRef?: string
  repositoryUrl?: string
  runGit: (args: string[]) => Promise<GitResult>
  runOfficialGit?: (args: string[]) => Promise<GitResult>
  trackingRef?: string
}

interface OfficialUpstreamTracking {
  ahead: number | null
  behind: number | null
  branch: string
  checkedAt: number
  error: string | null
  fetchedAt: number | null
  identityDirty: boolean
  identitySource: InstalledIdentity['source']
  installedRepository: string | null
  installedSha: string | null
  message: string | null
  readOnly: true
  repository: string
  state: 'error' | 'ready' | 'stale'
  targetSha: string | null
  trackingRef: string
}

interface LegacyUpdateSafety {
  allowed: boolean
  message: string | null
  reason: 'fork-divergent' | 'identity-dirty' | 'upstream-unproven' | null
}

type LegacyUpdateSafetySubject = 'package' | 'update-target'

// Resolve where the comparison cache may read the installed commit's history.
// A stamped repository is immutable build provenance and is safe to contact.
// Legacy schema-v1 stamps have no such provenance, so they may only read from
// the local checkout; inferring its mutable origin could disclose a fork-only
// stamped SHA to the official upstream server.
function resolveIdentityHistorySource(identity: InstalledIdentity, checkoutPath: string | null): string | null {
  if (identity.source === 'install-stamp' && identity.repository) {
    return `https://github.com/${identity.repository}.git`
  }

  return checkoutPath
}

function normalizeCommit(value: unknown): string | null {
  const commit = typeof value === 'string' ? value.trim() : ''

  if (!COMMIT_RE.test(commit) || /^0+$/.test(commit)) {
    return null
  }

  return commit
}

function normalizeRepository(value: unknown): string | null {
  const repository =
    typeof value === 'string'
      ? value
          .trim()
          .replace(/^\/+|\/+$/g, '')
          .replace(/\.git$/i, '')
      : ''

  return REPOSITORY_RE.test(repository) ? repository : null
}

function normalizeGitHubRepository(value: unknown): string | null {
  const direct = normalizeRepository(value)

  if (direct) {
    return direct
  }

  const remote = typeof value === 'string' ? value.trim() : ''
  let repository = ''

  if (remote.startsWith('git@github.com:')) {
    repository = remote.slice('git@github.com:'.length)
  } else if (remote.startsWith('ssh://git@github.com/')) {
    repository = remote.slice('ssh://git@github.com/'.length)
  } else {
    try {
      const parsed = new URL(remote)

      if (parsed.hostname.toLowerCase() !== 'github.com') {
        return null
      }

      repository = parsed.pathname
    } catch {
      return null
    }
  }

  return normalizeRepository(repository)
}

// A packaged app is identified by the commit embedded at build time, never by
// whatever mutable checkout happens to be under HERMES_HOME today. Dev runs
// have no package stamp, so they retain a clearly-labelled HEAD fallback.
function resolveInstalledIdentity({
  checkoutBranch,
  checkoutDirty,
  checkoutHead,
  checkoutRepository,
  installStamp,
  packaged
}: ResolveInstalledIdentityOptions): InstalledIdentity {
  const checkoutCommit = normalizeCommit(checkoutHead)
  const stampedCommit = normalizeCommit(installStamp?.commit)

  if (!packaged && checkoutCommit) {
    return {
      branch: typeof checkoutBranch === 'string' ? checkoutBranch.trim() || null : null,
      dirty: Boolean(checkoutDirty),
      error: null,
      repository: normalizeGitHubRepository(checkoutRepository),
      sha: checkoutCommit,
      source: 'checkout-head'
    }
  }

  if (stampedCommit) {
    const dirty = Boolean(installStamp?.dirty)

    return {
      branch: typeof installStamp?.branch === 'string' ? installStamp.branch.trim() || null : null,
      dirty,
      error: dirty ? 'dirty-install-stamp' : null,
      repository: normalizeGitHubRepository(installStamp?.repository),
      sha: dirty ? null : stampedCommit,
      source: 'install-stamp'
    }
  }

  if (packaged) {
    return {
      branch: null,
      dirty: false,
      error: 'install-stamp-missing',
      repository: null,
      sha: null,
      source: null
    }
  }

  return {
    branch: null,
    dirty: false,
    error: 'installed-identity-unavailable',
    repository: null,
    sha: null,
    source: null
  }
}

function firstLine(text: string): string {
  return (text || '').split('\n').find(Boolean) || ''
}

function trackingError(
  identity: InstalledIdentity,
  checkedAt: number,
  error: string,
  message: string,
  options: {
    branch: string
    repository: string
    targetSha?: string | null
    trackingRef: string
  }
): OfficialUpstreamTracking {
  return {
    ahead: null,
    behind: null,
    branch: options.branch,
    checkedAt,
    error,
    fetchedAt: null,
    identityDirty: identity.dirty,
    identitySource: identity.source,
    installedRepository: identity.repository,
    installedSha: identity.sha,
    message,
    readOnly: true,
    repository: options.repository,
    state: 'error',
    targetSha: options.targetSha ?? null,
    trackingRef: options.trackingRef
  }
}

// Fetch official main into a private ref that is unrelated to origin's
// configured refspec. This is deliberately read-only with respect to the
// checkout: it does not alter remotes, branches, the index, or the worktree.
//
// On a network failure an already-fetched private ref remains useful, but is
// labelled stale. No cached/missing ref is ever translated into a false zero.
async function inspectOfficialUpstream({
  branch = OFFICIAL_UPSTREAM_BRANCH,
  identity,
  identityRepositoryUrl = null,
  now = Date.now,
  officialCacheUrl = null,
  repository = OFFICIAL_UPSTREAM_REPOSITORY,
  repositoryRef = `refs/heads/${branch}`,
  repositoryUrl = OFFICIAL_UPSTREAM_URL,
  runGit,
  runOfficialGit,
  trackingRef = OFFICIAL_UPSTREAM_REF
}: InspectOfficialUpstreamOptions): Promise<OfficialUpstreamTracking> {
  const checkedAt = now()
  const errorOptions = { branch, repository, trackingRef }

  if (!identity.sha) {
    return trackingError(
      identity,
      checkedAt,
      identity.error || 'installed-identity-unavailable',
      identity.error === 'install-stamp-missing'
        ? 'The packaged app has no valid install stamp, so its upstream distance cannot be proven.'
        : identity.error === 'dirty-install-stamp'
          ? 'The packaged app was built from a dirty source tree, so its exact upstream distance cannot be proven.'
          : 'The installed desktop commit could not be resolved.',
      errorOptions
    )
  }

  let fetchedAt: number | null = null
  let fetchError: string | null = null
  let officialFetched = false

  const officialGit = runOfficialGit ?? runGit

  const officialIdentityHydration =
    Boolean(runOfficialGit && officialCacheUrl && identityRepositoryUrl) &&
    identity.repository?.toLowerCase() === repository.toLowerCase()

  const fetchIdentityRef = async (sourceRef: string): Promise<GitResult> => {
    if (!identityRepositoryUrl) {
      return { code: 1, stderr: 'The installed identity has no history source.', stdout: '' }
    }

    if (!officialIdentityHydration) {
      return runGit([
        'fetch',
        '--quiet',
        '--no-tags',
        identityRepositoryUrl,
        `+${sourceRef}:${INSTALLED_BUILD_REF}`
      ])
    }

    // Never let the mixed comparison cache negotiate directly with official
    // GitHub. Fetch the official build ref in the official-only cache, then
    // import that local ref into the comparison cache.
    const fetched = await officialGit([
      'fetch',
      '--quiet',
      '--no-tags',
      identityRepositoryUrl,
      `+${sourceRef}:${INSTALLED_BUILD_REF}`
    ])

    if (fetched.code !== 0) {
      return fetched
    }

    return runGit([
      'fetch',
      '--quiet',
      '--no-tags',
      officialCacheUrl as string,
      `+${INSTALLED_BUILD_REF}:${INSTALLED_BUILD_REF}`
    ])
  }

  try {
    const fetched = await officialGit([
      'fetch',
      '--quiet',
      '--no-tags',
      repositoryUrl,
      `+${repositoryRef}:${trackingRef}`
    ])

    if (fetched.code === 0) {
      officialFetched = true
    } else {
      fetchError = firstLine(fetched.stderr) || 'git fetch failed.'
    }
  } catch (error) {
    fetchError = error instanceof Error ? error.message : String(error)
  }

  // Production uses a separate official-only bare repository for the network
  // fetch, then imports that public ref locally. This prevents Git negotiation
  // from disclosing private fork commit IDs to the official upstream server.
  if (runOfficialGit && officialCacheUrl) {
    try {
      const imported = await runGit([
        'fetch',
        '--quiet',
        '--no-tags',
        officialCacheUrl,
        `+${trackingRef}:${trackingRef}`
      ])

      if (imported.code !== 0 && !fetchError) {
        fetchError = firstLine(imported.stderr) || 'The official-upstream cache could not be imported.'
      }

      if (officialFetched && imported.code === 0) {
        fetchedAt = checkedAt
      }
    } catch (error) {
      if (!fetchError) {
        fetchError = error instanceof Error ? error.message : String(error)
      }
    }
  } else if (officialFetched) {
    fetchedAt = checkedAt
  }

  let installed = await runGit(['cat-file', '-e', `${identity.sha}^{commit}`])

  if (installed.code !== 0 && identityRepositoryUrl) {
    const exactFetch = await fetchIdentityRef(identity.sha)

    if (exactFetch.code !== 0 && identity.branch) {
      await fetchIdentityRef(`refs/heads/${identity.branch}`)
    }

    installed = await runGit(['cat-file', '-e', `${identity.sha}^{commit}`])
  }

  if (installed.code !== 0) {
    return trackingError(
      identity,
      checkedAt,
      'installed-commit-unavailable',
      `The packaged commit ${identity.sha.slice(0, 12)} is not available in the isolated tracking repository.`,
      errorOptions
    )
  }

  const target = await runGit(['rev-parse', '--verify', `${trackingRef}^{commit}`])
  const targetSha = target.code === 0 ? firstLine(target.stdout).trim() : ''

  if (!targetSha) {
    return trackingError(
      identity,
      checkedAt,
      fetchError ? 'fetch-failed' : 'upstream-ref-unavailable',
      fetchError || 'The dedicated official-upstream ref could not be resolved.',
      errorOptions
    )
  }

  let mergeBase = await runGit(['merge-base', identity.sha, trackingRef])

  // A previous shallow/partial cache can contain the stamped tip without its
  // parents. Hydrate the stamped branch into the isolated repository and retry
  // before declaring the history unprovable. This never changes the app's
  // checkout or remotes.
  if ((mergeBase.code !== 0 || !firstLine(mergeBase.stdout)) && identityRepositoryUrl) {
    const identityRef = identity.branch ? `refs/heads/${identity.branch}` : identity.sha

    await fetchIdentityRef(identityRef)

    mergeBase = await runGit(['merge-base', identity.sha, trackingRef])
  }

  if (mergeBase.code !== 0 || !firstLine(mergeBase.stdout)) {
    return trackingError(
      identity,
      checkedAt,
      'history-unavailable',
      'The packaged commit and official upstream do not have enough shared history for an exact count.',
      { ...errorOptions, targetSha }
    )
  }

  // With official upstream on the left and the installed commit on the right,
  // rev-list reports "behind ahead". Fork-only CloudSeed patches therefore do
  // not inflate the upstream-behind number.
  const counts = await runGit(['rev-list', '--left-right', '--count', `${trackingRef}...${identity.sha}`])
  const [behindText, aheadText] = counts.stdout.trim().split(/\s+/)
  const behind = Number.parseInt(behindText || '', 10)
  const ahead = Number.parseInt(aheadText || '', 10)

  if (counts.code !== 0 || !Number.isFinite(behind) || !Number.isFinite(ahead)) {
    return trackingError(
      identity,
      checkedAt,
      'count-failed',
      firstLine(counts.stderr) || 'The official-upstream commit distance could not be counted.',
      { ...errorOptions, targetSha }
    )
  }

  return {
    ahead,
    behind,
    branch,
    checkedAt,
    error: fetchError ? 'fetch-failed' : null,
    fetchedAt,
    identityDirty: identity.dirty,
    identitySource: identity.source,
    installedRepository: identity.repository,
    installedSha: identity.sha,
    message: fetchError,
    readOnly: true,
    repository,
    state: fetchError ? 'stale' : 'ready',
    targetSha,
    trackingRef
  }
}

function resolveLegacyUpdateSafety(
  packaged: boolean,
  tracking: OfficialUpstreamTracking,
  subject: LegacyUpdateSafetySubject = 'package'
): LegacyUpdateSafety {
  if (!packaged) {
    return { allowed: true, message: null, reason: null }
  }

  if (tracking.identityDirty) {
    return {
      allowed: false,
      message:
        subject === 'update-target'
          ? 'Automatic updates are disabled because the mutable Hermes checkout has tracked local changes.'
          : 'Automatic updates are disabled because this packaged app was built from a dirty source tree.',
      reason: 'identity-dirty'
    }
  }

  if (tracking.ahead === null || tracking.state === 'error') {
    return {
      allowed: false,
      message:
        subject === 'update-target'
          ? 'Automatic updates are disabled until the mutable Hermes checkout can be compared safely with official upstream.'
          : 'Automatic updates are disabled until the packaged app can be compared safely with official upstream.',
      reason: 'upstream-unproven'
    }
  }

  if (tracking.ahead > 0) {
    return {
      allowed: false,
      message:
        subject === 'update-target'
          ? `Automatic updates are disabled because the mutable Hermes checkout has ${tracking.ahead} fork-only commit${tracking.ahead === 1 ? '' : 's'}.`
          : `Automatic updates are disabled because this build has ${tracking.ahead} fork-only commit${tracking.ahead === 1 ? '' : 's'}.`,
      reason: 'fork-divergent'
    }
  }

  return { allowed: true, message: null, reason: null }
}

export {
  inspectOfficialUpstream,
  INSTALLED_BUILD_REF,
  normalizeGitHubRepository,
  OFFICIAL_UPSTREAM_BRANCH,
  OFFICIAL_UPSTREAM_REF,
  OFFICIAL_UPSTREAM_REPOSITORY,
  OFFICIAL_UPSTREAM_URL,
  resolveBehindCount,
  resolveIdentityHistorySource,
  resolveInstalledIdentity,
  resolveLegacyUpdateSafety,
  shouldCountCommits
}
export type { GitResult, InstalledIdentity, OfficialUpstreamTracking }

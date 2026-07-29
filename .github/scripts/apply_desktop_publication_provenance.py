#!/usr/bin/env python3
"""Apply the reviewed Desktop publication-provenance change exactly once."""
from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def replace_once(relative: str, old: str, new: str) -> None:
    path = ROOT / relative
    text = path.read_text(encoding="utf-8")
    count = text.count(old)
    if count != 1:
        raise SystemExit(f"{relative}: expected one reviewed fragment, found {count}")
    path.write_text(text.replace(old, new, 1), encoding="utf-8")


# ---------------------------------------------------------------------------
# Build stamp: preserve schema 1, add optional publication repository and
# reject synthetic PR merge refs as future update branches.
# ---------------------------------------------------------------------------
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    ' *     "branch":        "<branch name>",\n *     "builtAt":       "<ISO 8601 UTC timestamp>",',
    ' *     "branch":        "<branch name>",\n *     "repository":    "<owner/repo>",\n *     "builtAt":       "<ISO 8601 UTC timestamp>",',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    'export const FALLBACK_BRANCH = "main"\n\nconst DESKTOP_ROOT',
    '''export const FALLBACK_BRANCH = "main"
export const FALLBACK_REPOSITORY = "NousResearch/hermes-agent"

export function normalizeRepository(value) {
  if (!value) return null
  let text = String(value).trim()

  if (text.startsWith("git@github.com:")) {
    text = text.slice("git@github.com:".length)
  } else if (text.startsWith("ssh://git@github.com/")) {
    text = text.slice("ssh://git@github.com/".length)
  } else {
    try {
      const parsed = new URL(text)
      if (parsed.hostname.toLowerCase() !== "github.com") return null
      text = parsed.pathname
    } catch {
      // owner/repo is already the canonical build-stamp form.
    }
  }

  text = text.replace(/^\\/+|\\/+$/g, "").replace(/\\.git$/i, "")
  return /^[0-9A-Za-z_.-]+\\/[0-9A-Za-z_.-]+$/.test(text) ? text : null
}

export function branchFromCI(env = process.env) {
  const explicit = String(env.HERMES_DESKTOP_UPDATE_BRANCH || "").trim()
  if (explicit) return explicit

  const head = String(env.GITHUB_HEAD_REF || "").trim()
  if (head) return head

  const refName = String(env.GITHUB_REF_NAME || "").trim()
  const refType = String(env.GITHUB_REF_TYPE || "").trim().toLowerCase()
  const syntheticPullRequest = /^\\d+\\/merge$/.test(refName) || refName.startsWith("refs/pull/")

  if (refName && !syntheticPullRequest && (refType === "branch" || !refType)) {
    return refName
  }

  return FALLBACK_BRANCH
}

const DESKTOP_ROOT''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''export function fromCI(env = process.env) {
  const sha = env.GITHUB_SHA
  if (!sha) return null
  const branch = env.GITHUB_REF_NAME || env.GITHUB_HEAD_REF || null
  return {
    commit: sha,
    branch: branch,
    dirty: false, // CI builds from a checkout-of-ref by definition
    source: "ci"
  }
}''',
    '''export function fromCI(env = process.env) {
  const sha = env.GITHUB_SHA
  if (!sha) return null
  const branch = branchFromCI(env)
  const repository =
    normalizeRepository(env.HERMES_DESKTOP_UPDATE_REPOSITORY || env.GITHUB_REPOSITORY) || FALLBACK_REPOSITORY
  return {
    commit: sha,
    branch,
    repository,
    dirty: false, // CI builds from a checkout-of-ref by definition
    source: "ci"
  }
}''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''  const branch = execFn("git rev-parse --abbrev-ref HEAD", { cwd: repoRoot })
  // `git status --porcelain -uno` is empty iff tracked files match HEAD.''',
    '''  const branch = execFn("git rev-parse --abbrev-ref HEAD", { cwd: repoRoot })
  const repository = normalizeRepository(execFn("git remote get-url origin", { cwd: repoRoot })) || FALLBACK_REPOSITORY
  // `git status --porcelain -uno` is empty iff tracked files match HEAD.''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''    commit: sha,
    branch: branch === "HEAD" ? null : branch, // detached HEAD -> null
    dirty: dirty,''',
    '''    commit: sha,
    branch: branch === "HEAD" ? null : branch, // detached HEAD -> null
    repository,
    dirty: dirty,''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    'export function fromFallback(branch = FALLBACK_BRANCH) {',
    'export function fromFallback(branch = FALLBACK_BRANCH, repository = FALLBACK_REPOSITORY) {',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''    commit: FALLBACK_COMMIT,
    branch: branch || FALLBACK_BRANCH,
    dirty: false,''',
    '''    commit: FALLBACK_COMMIT,
    branch: branch || FALLBACK_BRANCH,
    repository: normalizeRepository(repository) || FALLBACK_REPOSITORY,
    dirty: false,''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''  execFn = tryExec,
  fallbackBranch = FALLBACK_BRANCH
} = {}) {
  return fromCI(env) || fromLocalGit(repoRoot, execFn) || fromFallback(fallbackBranch)
}''',
    '''  execFn = tryExec,
  fallbackBranch = FALLBACK_BRANCH,
  fallbackRepository = FALLBACK_REPOSITORY
} = {}) {
  return fromCI(env) || fromLocalGit(repoRoot, execFn) || fromFallback(fallbackBranch, fallbackRepository)
}''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''    commit: stamp.commit,
    branch: stamp.branch,
    builtAt:''',
    '''    commit: stamp.commit,
    branch: stamp.branch,
    repository: stamp.repository,
    builtAt:''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.mjs",
    '''      " -> " +
      stamp.commit.slice(0, 12) +''',
    '''      " -> " +
      stamp.repository + "@" +
      stamp.commit.slice(0, 12) +''',
)

# Electron loader retains schema 1 compatibility but carries repository through.
replace_once(
    "apps/desktop/electron/main.ts",
    '//   { schemaVersion: 1, commit, branch, builtAt, dirty, source }',
    '//   { schemaVersion: 1, commit, branch, repository?, builtAt, dirty, source }',
)
replace_once(
    "apps/desktop/electron/main.ts",
    '''          commit: parsed.commit,
          branch: parsed.branch || null,
          builtAt:''',
    '''          commit: parsed.commit,
          branch: parsed.branch || null,
          repository: parsed.repository || null,
          builtAt:''',
)
replace_once(
    "apps/desktop/electron/main.ts",
    '''    `[hermes] install stamp: ${INSTALL_STAMP.commit.slice(0, 12)}${INSTALL_STAMP.branch ? ` (${INSTALL_STAMP.branch})` : ''}${INSTALL_STAMP.dirty ? ' [DIRTY]' : ''} from ${INSTALL_STAMP.source || 'unknown'}`''',
    '''    `[hermes] install stamp: ${INSTALL_STAMP.repository ? `${INSTALL_STAMP.repository}@` : ''}${INSTALL_STAMP.commit.slice(0, 12)}${INSTALL_STAMP.branch ? ` (${INSTALL_STAMP.branch})` : ''}${INSTALL_STAMP.dirty ? ' [DIRTY]' : ''} from ${INSTALL_STAMP.source || 'unknown'}`''',
)

# ---------------------------------------------------------------------------
# Bootstrap: validate publication owner/repo, fetch the installer from that
# repository, pass the same provenance to install.sh/install.ps1, and record it.
# ---------------------------------------------------------------------------
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''const FALLBACK_COMMIT_RE = /^0{7,40}$/
const FALLBACK_BRANCH = 'main'

function isPinnedCommit(commit) {
  return typeof commit === 'string' && STAMP_COMMIT_RE.test(commit) && !FALLBACK_COMMIT_RE.test(commit)
}''',
    '''const FALLBACK_COMMIT_RE = /^0{7,40}$/
const FALLBACK_BRANCH = 'main'
const FALLBACK_REPOSITORY = 'NousResearch/hermes-agent'
const INSTALL_REPOSITORY_RE = /^[0-9A-Za-z_.-]+\\/[0-9A-Za-z_.-]+$/

function isPinnedCommit(commit) {
  return typeof commit === 'string' && STAMP_COMMIT_RE.test(commit) && !FALLBACK_COMMIT_RE.test(commit)
}

function normalizeInstallRepository(value) {
  const repository = String(value || '').trim().replace(/^\\/+|\\/+$/g, '').replace(/\\.git$/i, '')
  return INSTALL_REPOSITORY_RE.test(repository) ? repository : null
}

function installRepositoryForStamp(installStamp) {
  return normalizeInstallRepository(installStamp && installStamp.repository) || FALLBACK_REPOSITORY
}

function repositoryCacheKey(repository, cacheKey) {
  if (repository === FALLBACK_REPOSITORY) {
    return cacheKey
  }

  return `${repository.replace(/[^0-9A-Za-z._-]/g, '_')}-${cacheKey}`
}''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    'function downloadInstallScript(ref, destPath) {',
    'function downloadInstallScript(ref, destPath, repository = FALLBACK_REPOSITORY) {',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  const scriptName = installScriptName()
  const url = `https://raw.githubusercontent.com/NousResearch/hermes-agent/${ref}/scripts/${scriptName}`''',
    '''  const scriptName = installScriptName()
  const safeRepository = normalizeInstallRepository(repository) || FALLBACK_REPOSITORY
  const url = `https://raw.githubusercontent.com/${safeRepository}/${ref}/scripts/${scriptName}`''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  const cached = cachedScriptPath(hermesHome, installRef.cacheKey)
  const resolvedCommit = installRef.pinned ? installRef.ref : null''',
    '''  const installRepository = installRepositoryForStamp(installStamp)
  const cached = cachedScriptPath(
    hermesHome,
    repositoryCacheKey(installRepository, installRef.cacheKey)
  )
  const resolvedCommit = installRef.pinned ? installRef.ref : null''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''      `[bootstrap] fetching ${installScriptName()} for ${installRef.ref.slice(0, 12)} from GitHub` +''',
    '''      `[bootstrap] fetching ${installScriptName()} for ${installRepository}@${installRef.ref.slice(0, 12)} from GitHub` +''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''    await _download(installRef.ref, cached)
    emit({ type: 'log', line: `[bootstrap] saved to ${cached}` })

    return { path: cached, source: 'download', commit: resolvedCommit, kind: installScriptKind() }''',
    '''    await _download(installRef.ref, cached, installRepository)
    emit({ type: 'log', line: `[bootstrap] saved to ${cached}` })

    return {
      path: cached,
      source: 'download',
      commit: resolvedCommit,
      repository: installRepository,
      kind: installScriptKind()
    }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''        return { path: cached, source: 'installed-agent', commit: resolvedCommit, kind: installScriptKind() }''',
    '''        return {
          path: cached,
          source: 'installed-agent',
          commit: resolvedCommit,
          repository: installRepository,
          kind: installScriptKind()
        }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''        return { path: installed, source: 'installed-agent', commit: resolvedCommit, kind: installScriptKind() }''',
    '''        return {
          path: installed,
          source: 'installed-agent',
          commit: resolvedCommit,
          repository: installRepository,
          kind: installScriptKind()
        }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''function spawnPowerShell(scriptPath, args, { emit, stageName, abortSignal, hermesHome }: any = {}) {''',
    '''function spawnPowerShell(
  scriptPath,
  args,
  { emit, stageName, abortSignal, hermesHome, installRepository }: any = {}
) {''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''          HERMES_HOME: hermesHome || process.env.HERMES_HOME || ''
        }''',
    '''          HERMES_HOME: hermesHome || process.env.HERMES_HOME || '',
          HERMES_INSTALL_REPOSITORY:
            installRepository || process.env.HERMES_INSTALL_REPOSITORY || FALLBACK_REPOSITORY
        }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''function spawnBash(scriptPath, args, { emit, stageName, abortSignal, hermesHome }: any = {}) {''',
    '''function spawnBash(scriptPath, args, { emit, stageName, abortSignal, hermesHome, installRepository }: any = {}) {''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''        HERMES_HOME: hermesHome || process.env.HERMES_HOME || ''
      }''',
    '''        HERMES_HOME: hermesHome || process.env.HERMES_HOME || '',
        HERMES_INSTALL_REPOSITORY:
          installRepository || process.env.HERMES_INSTALL_REPOSITORY || FALLBACK_REPOSITORY
      }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''async function fetchManifest({ scriptPath, installerKind, emit, hermesHome, activeRoot, installStamp, pinCommit }) {''',
    '''async function fetchManifest({
  scriptPath,
  installerKind,
  emit,
  hermesHome,
  activeRoot,
  installStamp,
  pinCommit,
  installRepository
}) {''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''    emit,
    stageName: '__manifest__',
    hermesHome
  })''',
    '''    emit,
    stageName: '__manifest__',
    hermesHome,
    installRepository
  })''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  installStamp,
  pinCommit
}) {''',
    '''  installStamp,
  pinCommit,
  installRepository
}) {''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''    stageName: stage.name,
    abortSignal,
    hermesHome
  })''',
    '''    stageName: stage.name,
    abortSignal,
    hermesHome,
    installRepository
  })''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  const runLog = openRunLog(logRoot || path.join(hermesHome, 'logs'))

  // Tee every event''',
    '''  const runLog = openRunLog(logRoot || path.join(hermesHome, 'logs'))
  const installRepository = installRepositoryForStamp(installStamp)

  // Tee every event''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''      `stamp=${installStamp ? installStamp.commit.slice(0, 12) : '<none>'}; ` +
      `runLog=${runLog.path}`''',
    '''      `stamp=${installStamp ? installStamp.commit.slice(0, 12) : '<none>'}; ` +
      `repository=${installRepository}; ` +
      `runLog=${runLog.path}`''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''      activeRoot,
      installStamp,
      pinCommit
    })''',
    '''      activeRoot,
      installStamp,
      pinCommit,
      installRepository
    })''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''        abortSignal,
        installStamp,
        pinCommit
      })''',
    '''        abortSignal,
        installStamp,
        pinCommit,
        installRepository
      })''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''    const markerPayload = {
      pinnedCommit,
      pinnedBranch: installStamp ? installStamp.branch : null
    }''',
    '''    const markerPayload = {
      pinnedCommit,
      pinnedBranch: installStamp ? installStamp.branch : null,
      pinnedRepository: installRepository
    }''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  installedAgentInstallScript,
  installRefForStamp,
  isPinnedCommit,''',
    '''  installedAgentInstallScript,
  installRefForStamp,
  installRepositoryForStamp,
  isPinnedCommit,
  normalizeInstallRepository,''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.ts",
    '''  parseStageResult,
  resolveCheckoutHead,''',
    '''  parseStageResult,
  repositoryCacheKey,
  resolveCheckoutHead,''',
)

# Installer clone origin follows the packaged publication provenance.
replace_once(
    "scripts/install.sh",
    '''REPO_URL_SSH="git@github.com:NousResearch/hermes-agent.git"
REPO_URL_HTTPS="https://github.com/NousResearch/hermes-agent.git"''',
    '''INSTALL_REPOSITORY="${HERMES_INSTALL_REPOSITORY:-NousResearch/hermes-agent}"
if [[ ! "$INSTALL_REPOSITORY" =~ ^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$ ]]; then
    echo "Invalid HERMES_INSTALL_REPOSITORY: expected owner/repo" >&2
    exit 2
fi
REPO_URL_SSH="git@github.com:${INSTALL_REPOSITORY}.git"
REPO_URL_HTTPS="https://github.com/${INSTALL_REPOSITORY}.git"''',
)
replace_once(
    "scripts/install.ps1",
    '''$RepoUrlSsh = "git@github.com:NousResearch/hermes-agent.git"
$RepoUrlHttps = "https://github.com/NousResearch/hermes-agent.git"''',
    '''$InstallRepository = if ($env:HERMES_INSTALL_REPOSITORY) { $env:HERMES_INSTALL_REPOSITORY.Trim() } else { "NousResearch/hermes-agent" }
if ($InstallRepository -notmatch '^[0-9A-Za-z_.-]+/[0-9A-Za-z_.-]+$') {
    throw "Invalid HERMES_INSTALL_REPOSITORY: expected owner/repo"
}
$RepoUrlSsh = "git@github.com:$InstallRepository.git"
$RepoUrlHttps = "https://github.com/$InstallRepository.git"''',
)

# ---------------------------------------------------------------------------
# Tests: exact branch/repository provenance and repository-aware bootstrap.
# ---------------------------------------------------------------------------
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  fromCI,''',
    '''  FALLBACK_BRANCH,
  FALLBACK_COMMIT,
  FALLBACK_REPOSITORY,
  branchFromCI,
  fromCI,''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''  isFallbackCommit,
  resolveStamp''',
    '''  isFallbackCommit,
  normalizeRepository,
  resolveStamp''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''test('fromCI reads GITHUB_SHA / GITHUB_REF_NAME', () => {
  assert.deepEqual(
    fromCI({ GITHUB_SHA: 'a'.repeat(40), GITHUB_REF_NAME: 'release' }),
    { commit: 'a'.repeat(40), branch: 'release', dirty: false, source: 'ci' }
  )
  assert.equal(fromCI({}), null)
})''',
    '''test('fromCI reads exact publication provenance', () => {
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

test('CI provenance never stamps a synthetic pull-request merge branch or a release tag', () => {
  assert.equal(
    branchFromCI({ GITHUB_REF_NAME: '123/merge', GITHUB_HEAD_REF: 'fix/desktop', GITHUB_REF_TYPE: 'branch' }),
    'fix/desktop'
  )
  assert.equal(branchFromCI({ GITHUB_REF_NAME: 'v0.18.0', GITHUB_REF_TYPE: 'tag' }), FALLBACK_BRANCH)
  assert.equal(branchFromCI({ HERMES_DESKTOP_UPDATE_BRANCH: 'main', GITHUB_REF_NAME: '123/merge' }), 'main')
  assert.equal(normalizeRepository('git@github.com:Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
  assert.equal(normalizeRepository('https://github.com/Dannyxlm/hermes-agent.git'), 'Dannyxlm/hermes-agent')
})''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git status --porcelain -uno')''',
    '''    if (cmd === 'git rev-parse --abbrev-ref HEAD') return 'main'
    if (cmd === 'git remote get-url origin') return 'git@github.com:Dannyxlm/hermes-agent.git'
    if (cmd === 'git status --porcelain -uno')''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''    commit: 'b'.repeat(40),
    branch: 'main',
    dirty: true,''',
    '''    commit: 'b'.repeat(40),
    branch: 'main',
    repository: 'Dannyxlm/hermes-agent',
    dirty: true,''',
)
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    dirty: false,''',
    '''    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,''',
)
# The fallback object appears twice in the test file.
replace_once(
    "apps/desktop/scripts/write-build-stamp.test.mjs",
    '''    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    dirty: false,''',
    '''    commit: FALLBACK_COMMIT,
    branch: FALLBACK_BRANCH,
    repository: FALLBACK_REPOSITORY,
    dirty: false,''',
)

replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''  installedAgentInstallScript,
  installRefForStamp,
  isPinnedCommit,''',
    '''  installedAgentInstallScript,
  installRefForStamp,
  installRepositoryForStamp,
  isPinnedCommit,
  repositoryCacheKey,''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''const ZERO_COMMIT = '0000000000000000000000000000000000000000'\n''',
    '''const ZERO_COMMIT = '0000000000000000000000000000000000000000'
const CLOUDSEED_REPOSITORY = 'Dannyxlm/hermes-agent'
''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {''',
    '''test('install repository provenance is validated and namespaces non-official cache entries', () => {
  assert.equal(installRepositoryForStamp({ repository: CLOUDSEED_REPOSITORY }), CLOUDSEED_REPOSITORY)
  assert.equal(installRepositoryForStamp({ repository: '../evil' }), 'NousResearch/hermes-agent')
  assert.equal(
    repositoryCacheKey(CLOUDSEED_REPOSITORY, 'fallback-main'),
    'Dannyxlm_hermes-agent-fallback-main'
  )
  assert.equal(repositoryCacheKey('NousResearch/hermes-agent', 'fallback-main'), 'fallback-main')
})

test('resolveMarkerPinnedCommit prefers real HEAD over fallback stamp zeros', () => {''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''    const logs = []
    const refs = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main' },''',
    '''    const logs = []
    const refs = []
    const repositories = []

    const result = await resolveInstallScript({
      installStamp: { commit: ZERO_COMMIT, branch: 'main', repository: CLOUDSEED_REPOSITORY },''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''      _download: async (ref, destPath) => {
        refs.push(ref)
        fs.mkdirSync''',
    '''      _download: async (ref, destPath, repository) => {
        refs.push(ref)
        repositories.push(repository)
        fs.mkdirSync''',
)
replace_once(
    "apps/desktop/electron/bootstrap-runner.test.ts",
    '''    assert.deepEqual(refs, ['main'])
    assert.equal(result.source, 'download')
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'fallback-main'))''',
    '''    assert.deepEqual(refs, ['main'])
    assert.deepEqual(repositories, [CLOUDSEED_REPOSITORY])
    assert.equal(result.source, 'download')
    assert.equal(result.repository, CLOUDSEED_REPOSITORY)
    assert.equal(result.commit, null)
    assert.equal(result.path, cachedScriptPath(home, 'Dannyxlm_hermes-agent-fallback-main'))''',
)

print("desktop publication provenance transformation applied")

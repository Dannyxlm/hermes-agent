/**
 * Write the desktop artifact stamp for source, bundled and Light builds.
 * bundle-electron-main.mjs bakes it into the running code. The packaged
 * sidecar exists only to detect replacement of that artifact by an update.
 * PM's bundle builder supplies relative launch paths for bundled artifacts.
 * Provenance comes from CI, local git, or an explicit unknown-source stamp.
 */

import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from "fs"
import { resolve, join, relative, posix } from "path"
import productIdentity from "../product-identity.cjs"
import { channelBuildRequest } from "../../../scripts/msix-shared.mjs"
import { validateBundleEnvironment } from "./bundle-env.mjs"
import { execFileSync } from "child_process"
import { createHash } from "crypto"

import { isMain } from "./utils.mjs"

const STAMP_SCHEMA_VERSION = 1

/** All-zero placeholder used when no real commit can be resolved. */
export const FALLBACK_COMMIT = "0000000000000000000000000000000000000000"
export const FALLBACK_BRANCH = "main"
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

  text = text.replace(/^\/+|\/+$/g, "").replace(/\.git$/i, "")
  return /^[0-9A-Za-z][0-9A-Za-z_.-]*\/[0-9A-Za-z][0-9A-Za-z_.-]*$/.test(text) ? text : null
}

export function branchFromCI(env = process.env) {
  const explicit = String(env.HERMES_DESKTOP_UPDATE_BRANCH || "").trim()
  if (explicit) return explicit

  const head = String(env.GITHUB_HEAD_REF || "").trim()
  if (head) return head

  const refName = String(env.GITHUB_REF_NAME || "").trim()
  const refType = String(env.GITHUB_REF_TYPE || "").trim().toLowerCase()
  const syntheticPullRequest = /^\d+\/merge$/.test(refName) || refName.startsWith("refs/pull/")

  if (refName && !syntheticPullRequest && (refType === "branch" || !refType)) {
    return refName
  }

  return FALLBACK_BRANCH
}

const DESKTOP_ROOT = resolve(import.meta.dirname, "..")
const REPO_ROOT = resolve(DESKTOP_ROOT, "..", "..")
const OUT_DIR = join(DESKTOP_ROOT, "build")
const OUT_FILE = join(OUT_DIR, "install-stamp.json")
const OVERLAY_MANIFEST = join(REPO_ROOT, "cloudseed", "hermes-overlays.v1.json")
const PUBLICATION_MANIFEST = join(REPO_ROOT, "cloudseed", "hermes-publication.v1.json")

function tryExec(argv, opts) {
  try {
    return execFileSync(argv[0], argv.slice(1), { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], timeout: 5000, ...opts }).trim()
  } catch {
    return null
  }
}

export function fromCI(env = process.env) {
  const sha = env.GITHUB_SHA
  if (!sha) return null
  const branch = branchFromCI(env)
  const repository =
    normalizeRepository(env.HERMES_DESKTOP_UPDATE_REPOSITORY || env.GITHUB_REPOSITORY) || FALLBACK_REPOSITORY
  const dirty = /^(?:1|true)$/i.test(String(env.HERMES_DESKTOP_UPDATE_DIRTY || "").trim())
  return {
    commit: sha,
    branch,
    repository,
    dirty,
    source: "ci"
  }
}

export function fromLocalGit(repoRoot = REPO_ROOT, execFn = tryExec) {
  const sha = execFn(["git", "rev-parse", "HEAD"], { cwd: repoRoot })
  if (!sha) return null
  const branch = execFn(["git", "rev-parse", "--abbrev-ref", "HEAD"], { cwd: repoRoot })
  const repository = normalizeRepository(execFn(["git", "remote", "get-url", "origin"], { cwd: repoRoot })) || FALLBACK_REPOSITORY
  // `git status --porcelain -uno` is empty iff tracked files match HEAD.
  // We exclude untracked files (-uno) intentionally: a developer who's
  // checked out an installer scratch dir alongside the repo shouldn't
  // poison every local build with a [DIRTY] stamp.  We DO care about
  // tracked-but-modified files because those mean the .exe content
  // differs from the commit being pinned.
  const status = execFn(["git", "status", "--porcelain", "-uno"], { cwd: repoRoot })
  const dirty = status !== null && status.length > 0
  return {
    commit: sha,
    branch: branch === "HEAD" ? null : branch, // detached HEAD -> null
    repository,
    dirty: dirty,
    source: "local"
  }
}

export function fromFallback(branch = FALLBACK_BRANCH, repository = FALLBACK_REPOSITORY) {
  // Non-git builds (ZIP download, bootstrap installer without a resolvable
  // HEAD) cannot determine a real commit.  Use a placeholder so local /
  // personal builds can still complete.  The desktop bootstrap treats the
  // all-zero commit as "unknown" and falls back to an unpinned branch
  // bootstrap instead of trying to fetch a non-existent GitHub commit.
  return {
    commit: FALLBACK_COMMIT,
    branch: branch || FALLBACK_BRANCH,
    repository: normalizeRepository(repository) || FALLBACK_REPOSITORY,
    dirty: false,
    source: "fallback"
  }
}

/**
 * Resolve the install stamp without writing it.  Pure enough for unit tests:
 * inject env / execFn / repoRoot to simulate CI, local git, or no-git trees.
 */
export function resolveStamp({
  env = process.env,
  repoRoot = REPO_ROOT,
  execFn = tryExec,
  fallbackBranch = FALLBACK_BRANCH,
  fallbackRepository = FALLBACK_REPOSITORY
} = {}) {
  const channelBuild = channelBuildRequest(env)
  if (channelBuild) {
    const local = fromLocalGit(repoRoot, execFn)
    if (!local || local.commit !== channelBuild.commit || local.dirty) throw new Error('Channel build identity does not match clean checkout HEAD')
    return { ...local, branch: null, source: 'channel-build', baseVersion: channelBuild.sourceVersion, channelBuild }
  }
  if (env.HERMES_BUILD_COMMIT) {
    if (!/^[a-f0-9]{40}$/.test(env.HERMES_BUILD_COMMIT) || env.HERMES_PAYLOAD_TAG) {
      throw new Error('Commit builds require an exact full SHA without a release tag')
    }
    const local = fromLocalGit(repoRoot, execFn)
    if (!local || local.commit !== env.HERMES_BUILD_COMMIT) {
      throw new Error('Commit build identity does not match the checkout HEAD')
    }
    return { ...local, branch: null, source: 'commit-build' }
  }
  return fromCI(env) || fromLocalGit(repoRoot, execFn) || fromFallback(fallbackBranch, fallbackRepository)
}

export function isFallbackCommit(commit) {
  return typeof commit === "string" && /^0{7,40}$/.test(commit)
}

function cleanEnv(value) {
  const text = String(value || "").trim()
  return text || null
}

export function releaseProvenance(
  env = process.env,
  manifestPath = OVERLAY_MANIFEST,
  publicationPath = PUBLICATION_MANIFEST
) {
  let overlayIds = []
  let overlayManifestSha256 = null
  let publication = null

  try {
    const raw = readFileSync(manifestPath)
    const manifest = JSON.parse(raw.toString("utf8"))
    const rows = Array.isArray(manifest?.overlays) ? manifest.overlays : []
    const ids = rows.map(row => String(row?.id || "").trim())

    if (
      manifest?.schema_version === "cloudseed-hermes-overlays.v1" &&
      ids.length > 0 &&
      ids.every(id => /^[a-z0-9][a-z0-9-]{0,79}$/.test(id)) &&
      new Set(ids).size === ids.length
    ) {
      overlayIds = ids
      overlayManifestSha256 = createHash("sha256").update(raw).digest("hex")
    }
  } catch {
    // Ordinary upstream builds have no CloudSeed overlay manifest.
  }

  try {
    const parsed = JSON.parse(readFileSync(publicationPath, "utf8"))
    const transition = parsed?.overlay_transition
    const transitioned = [
      ...(Array.isArray(transition?.added) ? transition.added : []),
      ...(Array.isArray(transition?.retained) ? transition.retained : [])
    ]
    const retired = Array.isArray(transition?.retired) ? transition.retired : []
    const transitionIsExact =
      transitioned.length === overlayIds.length &&
      new Set(transitioned).size === transitioned.length &&
      new Set(retired).size === retired.length &&
      transitioned.every(id => overlayIds.includes(id)) &&
      overlayIds.every(id => transitioned.includes(id)) &&
      retired.every(id => !overlayIds.includes(id))

    if (
      parsed?.schema_version === "cloudseed-hermes-publication.v1" &&
      /^[0-9a-f]{40}$/i.test(String(parsed.official_revision || "")) &&
      normalizeRepository(parsed.official_repository) === "NousResearch/hermes-agent" &&
      normalizeRepository(parsed.integration_repository) === "Dannyxlm/hermes-agent" &&
      parsed.official_branch === "main" &&
      parsed.integration_branch === "main" &&
      transitionIsExact
    ) {
      publication = parsed
    }
  } catch {
    // Ordinary upstream builds have no managed publication manifest.
  }

  const officialUpstreamCommit = cleanEnv(env.HERMES_OFFICIAL_UPSTREAM_COMMIT) || publication?.official_revision
  const selfUpdateOverride = cleanEnv(env.HERMES_DESKTOP_SELF_UPDATE_ALLOWED)

  return {
    releaseId: cleanEnv(env.HERMES_RELEASE_ID) || publication?.release_id || null,
    officialUpstreamCommit:
      officialUpstreamCommit && /^[0-9a-f]{40}$/i.test(officialUpstreamCommit)
        ? officialUpstreamCommit.toLowerCase()
        : null,
    officialReleaseTag: cleanEnv(env.HERMES_OFFICIAL_RELEASE_TAG) || publication?.official_release_tag || null,
    overlayIds,
    overlayManifestSha256,
    installMode:
      cleanEnv(env.HERMES_DESKTOP_INSTALL_MODE) || publication?.desktop_install_mode || "source",
    selfUpdateAllowed:
      selfUpdateOverride !== null
        ? /^(?:1|true)$/i.test(selfUpdateOverride)
        : publication?.desktop_self_update_allowed === true
  }
}

/** Qualify desktop CLI files before the immutable runtime paths are baked.
 * Canonical command keys remain stable for backend consumers; public aliases
 * come from the declared filenames, never those internal keys.
 */
export function stageDesktopLaunchers(root, identity = productIdentity) {
  const file = join(root, 'manifest.json')
  const manifest = JSON.parse(readFileSync(file, 'utf8'))
  const windows = manifest.target.startsWith('win32')
  const commands = {}
  const launchers = []
  for (const [name, source] of Object.entries(manifest.runtime.commands)) {
    const alias = name.replace(/^hermes(?=-|$)/, identity.cliName)
    const destination = posix.join(posix.dirname(source), `${alias}${windows ? '.exe' : ''}`)
    if (source !== destination && existsSync(join(root, source))) {
      renameSync(join(root, source), join(root, destination))
    }
    if (!existsSync(join(root, destination))) throw new Error(`Missing desktop launcher: ${destination}`)
    commands[name] = destination
    launchers.push(alias)
  }
  manifest.runtime.commands = commands
  manifest.launchers = launchers
  writeFileSync(file, JSON.stringify(manifest, null, 2) + '\n')
  return manifest
}

/** Electron and the embedded CLI must see the same immutable provenance. */
export function writeDesktopStamp(outDir, built) {
  const json = JSON.stringify(built, null, 2) + "\n"
  mkdirSync(outDir, { recursive: true })
  if (built.payload === 'bundled') {
    writeFileSync(join(outDir, 'agent-payload', built.runtime.repoDir, 'install-stamp.json'), json, 'utf8')
  }
  writeFileSync(join(outDir, 'install-stamp.json'), json, 'utf8')
}

function main() {
  const stamp = resolveStamp()
  if (!stamp || !stamp.commit) {
    // Should not happen — fromFallback() always provides a commit.
    console.error(
      "[write-build-stamp] ERROR: could not determine git commit.\n" +
        "  - $GITHUB_SHA not set\n" +
        "  - `git rev-parse HEAD` failed at " +
        REPO_ROOT +
        "\n" +
        "Packaged builds require a git ref to pin first-launch install.ps1\n" +
        "against. Run from a git checkout or set $GITHUB_SHA explicitly."
    )
    process.exit(1)
  }

  if (isFallbackCommit(stamp.commit)) {
    console.warn(
      "[write-build-stamp] WARNING: no git commit found (non-git checkout?).\n" +
        "  Using placeholder commit — the packaged app will fall back to the\n" +
        "  default branch for first-launch bootstrap.  For production builds,\n" +
        "  run from a git checkout or set $GITHUB_SHA."
    )
  }

  if (stamp.dirty) {
    console.warn(
      "[write-build-stamp] WARNING: working tree is dirty.\n" +
        "  Pinning to " +
        stamp.commit.slice(0, 12) +
        " but the packaged code may differ from that commit.\n" +
        "  Commit your changes before publishing this build."
    )
  }

  const bundled = ['bundled', 'store'].includes(process.env.HERMES_DESKTOP_VARIANT)
  const payload = bundled
    ? stageDesktopLaunchers(join(OUT_DIR, 'agent-payload'))
    : null
  const built = buildStampPayload(stamp, process.env, process.platform, payload)
  writeDesktopStamp(OUT_DIR, { ...built, integrationCommit: stamp.commit, repository: stamp.repository, ...releaseProvenance() })
  console.log(
    "[write-build-stamp] wrote " +
      relative(REPO_ROOT, OUT_FILE) +
      " -> " +
      stamp.repository + "@" +
      stamp.commit.slice(0, 12) +
      (stamp.branch ? " (" + stamp.branch + ")" : "") +
      (stamp.dirty ? " [DIRTY]" : "") +
      (stamp.source === "fallback" ? " [FALLBACK]" : "")
  )
}

/** One artifact schema for source, bundled and Light builds.
 * The PM bundle builder supplies launch paths only for bundled artifacts.
 */
export function buildStampPayload(stamp, env = process.env, platform = process.platform, payload = null) {
  const variant = (env.HERMES_DESKTOP_VARIANT || "").trim()
  const channelBuild = channelBuildRequest(env)
  if (channelBuild && (stamp.commit !== channelBuild.commit || stamp.dirty)) throw new Error('Channel build identity does not match stamp')
  const commitBuild = env.HERMES_BUILD_COMMIT || null
  if (commitBuild && (!/^[a-f0-9]{40}$/.test(commitBuild) || commitBuild !== stamp.commit)) {
    throw new Error('Commit build identity does not match the stamp commit')
  }
  if (commitBuild && env.HERMES_PAYLOAD_TAG) {
    throw new Error('Commit builds cannot also set a release tag')
  }
  const version = env.HERMES_PAYLOAD_VERSION || (env.HERMES_PAYLOAD_TAG || '').replace(/^v/, '') || null
  // The bundle's baked runtime defaults/clears, recorded as data so the smoke
  // driver can predict the app's resolved Hermes home without reimplementing
  // the banner. Only commit bundles carry one, but the field is harmless when
  // absent elsewhere.
  const bundleEnv = env.HERMES_BUNDLE_ENV_JSON ? validateBundleEnvironment(JSON.parse(env.HERMES_BUNDLE_ENV_JSON)) : undefined
  const base = {
    schemaVersion: STAMP_SCHEMA_VERSION,
    commit: stamp.commit,
    branch: commitBuild || channelBuild ? null : stamp.branch,
    builtAt: new Date().toISOString(),
    dirty: stamp.dirty,
    source: channelBuild ? 'channel-build' : commitBuild ? 'commit-build' : stamp.source,
    commitDate: stamp.commitDate ?? null,
    baseVersion: channelBuild?.sourceVersion ?? stamp.baseVersion ?? version?.split('-')[0] ?? null,
    displayVersion: channelBuild
      ? `${channelBuild.sourceVersion} (${channelBuild.channel} #${channelBuild.sequence}, ${channelBuild.commit.slice(0, 7)})`
      : stamp.displayVersion ?? version,
    distance: stamp.distance ?? null
  }

  if (channelBuild) base.channelBuild = channelBuild
  // Rehearsal receivers use the real stable update path, never preview resolution.
  if (channelBuild?.receiverCandidate) {
    delete base.channelBuild
    base.source = 'build'
    base.displayVersion = channelBuild.version
  }
  if (variant === 'bundled') base.receiverProtocol = 1

  const updateMechanism = {
    '': 'self',
    bootstrap: 'self',
    store: 'microsoft-store',
    bundled: { win32: 'app-installer', darwin: 'electron-updater' }[platform] || 'external',
    light: platform === 'darwin' ? 'electron-updater' : 'external'
  }[variant]
  if (!updateMechanism) throw new Error(`Unknown desktop variant: ${variant}`)
  if (channelBuild && updateMechanism === 'external') throw new Error('Channel builds require a supported native update owner')
  const bundled = variant === 'bundled' || variant === 'store'
  if (bundled && !payload?.runtime?.commands?.hermes) {
    throw new Error('PM payload has no completed launch contract; stage the bundle before packaging')
  }
  return {
    ...base,
    payload: variant === "store" ? "bundled" : variant || "bootstrap",
    distribution: "desktop-app",

    updateMechanism: commitBuild ? 'external' : updateMechanism,
    tag: channelBuild?.receiverCandidate ? channelBuild.releaseTag : env.HERMES_PAYLOAD_TAG || null,
    ...(bundleEnv ? { bundleEnv } : {}),
    ...(bundled ? { runtime: payload.runtime } : {})
  }
}

if (isMain(import.meta.url)) {
  main()
}

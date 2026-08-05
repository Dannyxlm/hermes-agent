/**
 * Writes apps/desktop/build/install-stamp.json with the git ref the desktop
 * .exe should pin to at first-launch bootstrap time.  This file ships inside
 * the packaged app via electron-builder's extraResources entry and is read
 * by electron/main.ts to drive the install.ps1 stage bootstrap flow.
 *
 * Schema (subject to bump via STAMP_SCHEMA_VERSION):
 *   {
 *     "schemaVersion": 1,
 *     "commit":        "<40-char SHA>",
 *     "branch":        "<branch name>",
 *     "repository":    "<owner/repo>",
 *     "builtAt":       "<ISO 8601 UTC timestamp>",
 *     "dirty":         true|false,
 *     "source":        "ci" | "local" | "fallback"
 *   }
 *
 * Source preference order:
 *   1. CI env vars ($GITHUB_SHA / $GITHUB_REF_NAME) -- avoid edge cases with
 *      shallow clones, detached HEADs, etc. in CI.
 *   2. Local `git rev-parse` against the parent repo (../..).
 *   3. Fallback stamp for local/personal builds from non-git source trees
 *      (ZIP extract, interrupted clone with no HEAD, etc.).
 *
 * Dev / out-of-repo builds without git produce an explicit fallback stamp
 * rather than aborting the whole build.  Bootstrap treats the all-zero
 * commit as unpinned and follows the branch instead of fetching a fake SHA.
 */

import { mkdirSync, readFileSync, writeFileSync } from "fs"
import { resolve, join, relative } from "path"
import { execSync } from "child_process"
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

function tryExec(cmd, opts) {
  try {
    return execSync(cmd, { encoding: "utf8", stdio: ["ignore", "pipe", "ignore"], ...opts }).trim()
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
  const sha = execFn("git rev-parse HEAD", { cwd: repoRoot })
  if (!sha) return null
  const branch = execFn("git rev-parse --abbrev-ref HEAD", { cwd: repoRoot })
  const repository = normalizeRepository(execFn("git remote get-url origin", { cwd: repoRoot })) || FALLBACK_REPOSITORY
  // `git status --porcelain -uno` is empty iff tracked files match HEAD.
  // We exclude untracked files (-uno) intentionally: a developer who's
  // checked out an installer scratch dir alongside the repo shouldn't
  // poison every local build with a [DIRTY] stamp.  We DO care about
  // tracked-but-modified files because those mean the .exe content
  // differs from the commit being pinned.
  const status = execFn("git status --porcelain -uno", { cwd: repoRoot })
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

  const provenance = releaseProvenance()
  const payload = {
    schemaVersion: STAMP_SCHEMA_VERSION,
    commit: stamp.commit,
    integrationCommit: stamp.commit,
    branch: stamp.branch,
    repository: stamp.repository,
    builtAt: new Date().toISOString(),
    dirty: stamp.dirty,
    source: stamp.source,
    ...provenance
  }

  mkdirSync(OUT_DIR, { recursive: true })
  writeFileSync(OUT_FILE, JSON.stringify(payload, null, 2) + "\n", "utf8")
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

if (isMain(import.meta.url)) {
  main()
}

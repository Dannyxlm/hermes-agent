/**
 * Renderer bundle generation check.
 *
 * `index.html` and the hashed chunks under `dist/assets/` are ONE generation:
 * every `lazy()` route resolves to a filename baked into that generation's
 * module graph. A self-update that replaces the package while its files are
 * locked (antivirus, a still-running instance, an interrupted Windows replace)
 * can leave the two copies electron-builder ships — inside `app.asar` and,
 * because `asarUnpack` lists `dist/**`, beside it in `app.asar.unpacked` —
 * from DIFFERENT generations. The window then loads an `index.html` whose
 * chunks are gone and dies on the first lazy import:
 *
 *   Failed to fetch dynamically imported module:
 *   …/app.asar/dist/assets/shiki-block-COiz1pEN.js
 *
 * The app looks permanently broken (every relaunch reloads the same torn copy),
 * yet the OTHER copy is usually intact. This makes that checkable, so the
 * loader can prefer a complete generation and only report a repair when both
 * are torn.
 *
 * Pure + injectable so it is testable without booting Electron. `fs` here is
 * Electron's asar-aware fs: paths inside `app.asar` read like real files.
 */

import fs from 'node:fs'
import path from 'node:path'

// The modules the browser fetches before any app code runs: Vite emits them as
// `<script type="module" src>` plus `<link rel="modulepreload" href>`.
const TAG_WITH_URL = /<(?:script|link)\b[^>]*\b(?:src|href)=["']([^"']+)["'][^>]*>/gi
const MODULE_TAG = /\btype=["']module["']|\brel=["']modulepreload["']/i
const RENDERER_MANIFEST = 'manifest.json'

interface RendererManifestChunk {
  file?: unknown
  isEntry?: unknown
  imports?: unknown
  dynamicImports?: unknown
  css?: unknown
  assets?: unknown
}

type RendererManifest = Record<string, RendererManifestChunk>

function normalizeLocalAssetRef(ref: string): string | null {
  if (/^[a-z]+:|^\/\//i.test(ref)) {
    return null
  }

  return ref.replace(/^\.\//, '').replace(/^\/+/, '').split(/[?#]/)[0]
}

export function parseModuleAssetRefs(html: string): string[] {
  const refs: string[] = []

  for (const [tag, href] of String(html ?? '').matchAll(TAG_WITH_URL)) {
    // Absolute/CDN URLs aren't part of this bundle's generation.
    const ref = normalizeLocalAssetRef(href)

    if (MODULE_TAG.test(tag) && ref) {
      refs.push(ref)
    }
  }

  return refs
}

function parseRendererManifest(raw: string): RendererManifest | null {
  try {
    const parsed: unknown = JSON.parse(raw)

    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return parsed as RendererManifest
    }
  } catch {
    // Older bundles have no manifest; torn/partial updates may leave one
    // unreadable. The caller falls back to the index.html-only check.
  }

  return null
}

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((item): item is string => typeof item === 'string') : []
}

interface RendererManifestGraph {
  assets: string[]
  unresolvedChunks: string[]
}

/**
 * Resolve the graph for the entry actually loaded by index.html. Vite's
 * manifest stores `imports` and `dynamicImports` as manifest keys, while
 * `file`, `css`, and `assets` are paths relative to the output directory.
 */
function rendererManifestGraph(manifest: RendererManifest, htmlRefs: string[]): RendererManifestGraph | null {
  const htmlRefSet = new Set(htmlRefs)

  const entryKeys = Object.entries(manifest)
    .filter(([, chunk]) => {
      const file = typeof chunk?.file === 'string' ? normalizeLocalAssetRef(chunk.file) : null

      return chunk?.isEntry === true && file !== null && htmlRefSet.has(file)
    })
    .map(([key]) => key)

  // A readable manifest from an older/different build shape must not brick
  // recovery. If it cannot identify this HTML's entry, retain the old check.
  if (entryKeys.length === 0) {
    return null
  }

  const assets = new Set<string>()
  const unresolvedChunks = new Set<string>()
  const visited = new Set<string>()

  const visit = (key: string) => {
    if (visited.has(key)) {
      return
    }

    visited.add(key)

    const chunk = manifest[key]

    if (!chunk || typeof chunk !== 'object' || typeof chunk.file !== 'string') {
      unresolvedChunks.add(key)

      return
    }

    for (const ref of [chunk.file, ...stringList(chunk.css), ...stringList(chunk.assets)]) {
      const normalized = normalizeLocalAssetRef(ref)

      if (normalized) {
        assets.add(normalized)
      }
    }

    for (const dependency of [...stringList(chunk.imports), ...stringList(chunk.dynamicImports)]) {
      visit(dependency)
    }
  }

  for (const key of entryKeys) {
    visit(key)
  }

  return { assets: [...assets], unresolvedChunks: [...unresolvedChunks] }
}

export interface RendererBundleDeps {
  readFileSync?: (file: string, encoding: 'utf8') => string
  existsSync?: (file: string) => boolean
}

/**
 * The files in `indexPath`'s active Vite entry graph that do not exist beside
 * it. This includes lazy dynamic chunks and each chunk's CSS/assets.
 *
 * Bundles built before the manifest was introduced, or whose manifest cannot
 * be read/parsed/matched to the active entry, retain the HTML-only check for
 * backward compatibility. Empty ⇒ a complete generation (or an index naming
 * nothing checkable — the caller's own existence gate owns unreadable/missing
 * files). Non-empty ⇒ torn.
 */
export function missingRendererAssets(indexPath: string, deps: RendererBundleDeps = {}): string[] {
  const { readFileSync = fs.readFileSync, existsSync = fs.existsSync } = deps
  const dir = path.dirname(indexPath)

  let html: string

  try {
    html = readFileSync(indexPath, 'utf8')
  } catch {
    return []
  }

  const htmlRefs = parseModuleAssetRefs(html)
  let manifestGraph: RendererManifestGraph | null = null

  try {
    const manifest = parseRendererManifest(readFileSync(path.join(dir, RENDERER_MANIFEST), 'utf8'))

    if (manifest) {
      manifestGraph = rendererManifestGraph(manifest, htmlRefs)
    }
  } catch {
    // Absent/unreadable manifests are expected for older renderer bundles.
  }

  const refs = new Set([...htmlRefs, ...(manifestGraph?.assets ?? [])])
  const missing = [...refs].filter(ref => !existsSync(path.join(dir, ref)))

  // A dependency key with no manifest record is also an incomplete graph. It
  // has no output filename to test, so identify the broken manifest edge.
  missing.push(...(manifestGraph?.unresolvedChunks.map(key => `manifest:${key}`) ?? []))

  return missing
}

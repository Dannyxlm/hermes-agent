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
const RENDERER_MANIFESTS = ['renderer-manifest.json', 'manifest.json'] as const

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

// Vite bakes each chunk's lazy-import filenames into an inline
// `__vite__mapDeps` table — `…(m.f || (m.f = ["assets/syntax-diff-….js", …]))` —
// and call sites reference the table by index (`__vite__mapDeps([0,1])`).
// The quoted filenames in that table are the chunks the generation will
// dynamically import at runtime (syntax-diff-*, shiki-*, mermaid-embed-*, …):
// exactly the files a torn update leaves dangling AFTER index.html's own
// preload list checks out. The index-only call sites contain no quoted
// strings, so this scan matches only the definition's filename table.
const MAP_DEPS_ARRAY = /__vite__mapDeps[^[\]]{0,300}\[([^\]]*)\]/g
const QUOTED_REF = /["']([^"']+)["']/g

export function parseLazyChunkRefs(js: string): string[] {
  const refs = new Set<string>()

  for (const [, body] of String(js ?? '').matchAll(MAP_DEPS_ARRAY)) {
    for (const [, ref] of body.matchAll(QUOTED_REF)) {
      // Absolute/CDN URLs aren't part of this bundle's generation.
      if (!/^[a-z]+:|^\/\//i.test(ref)) {
        refs.add(ref.replace(/^\.\//, '').split(/[?#]/)[0])
      }
    }
  }

  return [...refs]
}

export interface RendererBundleDeps {
  readFileSync?: (file: string, encoding: 'utf8') => string
  existsSync?: (file: string) => boolean
}

/** Vite's build-time graph avoids opening megabytes of lazy chunks at startup.
 * Only trust a manifest paired with this index's entry and preload generation.
 * Old builds, malformed manifests and interrupted replacements retain the
 * existing JS graph walk below; a manifest is never an existence-only bypass.
 */
function manifestAssetRefs(
  indexPath: string,
  bootRefs: string[],
  readFileSync: NonNullable<RendererBundleDeps['readFileSync']>
): string[] | null {
  try {
    const manifest = JSON.parse(readFileSync(path.join(path.dirname(indexPath), 'renderer-manifest.json'), 'utf8'))

    if (!manifest || typeof manifest !== 'object' || Array.isArray(manifest)) {
      return null
    }

    const entry = manifest['index.html']
    const normalize = (ref: string) => ref.replace(/^\.?\//, '')

    if (!entry?.isEntry || !bootRefs.map(normalize).includes(entry.file)) {
      return null
    }

    const refs = new Set<string>()

    const localRef = (ref: unknown): ref is string =>
      typeof ref === 'string' && ref.length > 0 && !/^[a-z]+:|^[/\\]/i.test(ref) && !ref.split(/[/\\]/).includes('..')

    for (const chunk of Object.values(manifest) as Record<string, unknown>[]) {
      if (!chunk || typeof chunk !== 'object' || !localRef(chunk.file)) {
        return null
      }

      refs.add(chunk.file)

      for (const key of ['imports', 'dynamicImports']) {
        const imports = chunk[key] ?? []

        if (!Array.isArray(imports) || !imports.every(ref => typeof ref === 'string' && Object.hasOwn(manifest, ref))) {
          return null
        }
      }

      for (const key of ['css', 'assets']) {
        const assets = chunk[key] ?? []

        if (!Array.isArray(assets) || !assets.every(localRef)) {
          return null
        }

        for (const ref of assets) {
          refs.add(ref)
        }
      }
    }

    if (!bootRefs.every(ref => refs.has(normalize(ref)))) {
      return null
    }

    return [...refs]
  } catch {
    return null
  }
}

/**
 * The files in `indexPath`'s active Vite entry graph that do not exist beside
 * it. This includes lazy dynamic chunks and each chunk's CSS/assets from the
 * Vite manifest, plus lazy chunks declared by inline `__vite__mapDeps` tables.
 * The inline graph walk keeps older or manifest-less bundles protected too.
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
  const manifestRefs = manifestAssetRefs(indexPath, htmlRefs, readFileSync)
  if (manifestRefs) {
    return manifestRefs.filter(ref => !existsSync(path.join(dir, ref)))
  }

  let manifestGraph: RendererManifestGraph | null = null
  try {
    const manifest = parseRendererManifest(readFileSync(path.join(dir, 'manifest.json'), 'utf8'))
    if (manifest) manifestGraph = rendererManifestGraph(manifest, htmlRefs)
  } catch {
    // Absent/unreadable manifests are expected for older renderer bundles.
  }

  const missing: string[] = []
  const seen = new Set<string>()
  const queue = [...new Set([...htmlRefs, ...(manifestGraph?.assets ?? [])])]

  while (queue.length > 0) {
    const ref = queue.shift()!

    if (seen.has(ref)) {
      continue
    }

    seen.add(ref)

    const file = path.join(dir, ref)

    if (!existsSync(file)) {
      missing.push(ref)

      continue
    }

    // A present JS chunk may still name lazy imports of its own. Read its
    // __vite__mapDeps table and queue those refs relative to the dir that
    // owns them (map-deps entries are emitted relative to the chunk's own
    // directory in older Vite output, and base-relative in newer output —
    // normalize both to index-dir-relative before the existence check).
    if (!/\.m?js$/i.test(ref)) {
      continue
    }

    let js: string

    try {
      js = readFileSync(file, 'utf8')
    } catch {
      // Unreadable-but-present is the existence gate's concern, same contract
      // as an unreadable index.html above.
      continue
    }

    for (const lazyRef of parseLazyChunkRefs(js)) {
      const chunkDirRelative = path.join(path.dirname(ref), lazyRef).split(path.sep).join('/')

      // Prefer whichever interpretation lands on a real file; when neither
      // does, report the index-dir-relative spelling (matches how the
      // boot-critical refs above are reported).
      if (existsSync(path.join(dir, lazyRef))) {
        queue.push(lazyRef)
      } else if (existsSync(path.join(dir, chunkDirRelative))) {
        queue.push(chunkDirRelative)
      } else {
        queue.push(lazyRef)
      }
    }
  }

  // A dependency key with no manifest record is also an incomplete graph. It
  // has no output filename to test, so identify the broken manifest edge.
  missing.push(...(manifestGraph?.unresolvedChunks.map(key => `manifest:${key}`) ?? []))

  return missing
}

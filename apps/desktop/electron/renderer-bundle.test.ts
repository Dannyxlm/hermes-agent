import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { missingRendererAssets, parseModuleAssetRefs, type RendererBundleDeps } from './renderer-bundle'

const tempDirs: string[] = []

afterEach(() => {
  for (const dir of tempDirs.splice(0)) {
    fs.rmSync(dir, { recursive: true, force: true })
  }
})

function makeBundle(files: string[], manifest?: unknown): string {
  const dir = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-renderer-bundle-'))
  tempDirs.push(dir)

  fs.mkdirSync(path.join(dir, 'assets'), { recursive: true })
  fs.writeFileSync(
    path.join(dir, 'index.html'),
    '<link rel="modulepreload" href="./assets/vendor.js"><script type="module" src="./assets/index.js"></script>'
  )

  for (const file of files) {
    fs.mkdirSync(path.dirname(path.join(dir, file)), { recursive: true })
    fs.writeFileSync(path.join(dir, file), file)
  }

  if (manifest !== undefined) {
    fs.writeFileSync(path.join(dir, 'manifest.json'), JSON.stringify(manifest))
  }

  return path.join(dir, 'index.html')
}

const manifest = {
  'index.html': {
    file: 'assets/index.js',
    isEntry: true,
    imports: ['_vendor.js'],
    dynamicImports: ['src/lazy.tsx'],
    css: ['assets/index.css'],
    assets: ['assets/logo.svg']
  },
  '_vendor.js': {
    file: 'assets/vendor.js'
  },
  'src/lazy.tsx': {
    file: 'assets/lazy.js',
    isDynamicEntry: true,
    imports: ['_lazy-dependency.js'],
    css: ['assets/lazy.css'],
    assets: ['assets/lazy.woff2']
  },
  '_lazy-dependency.js': {
    file: 'assets/lazy-dependency.js'
  }
}

const completeGraph = [
  'assets/index.js',
  'assets/vendor.js',
  'assets/index.css',
  'assets/logo.svg',
  'assets/lazy.js',
  'assets/lazy.css',
  'assets/lazy.woff2',
  'assets/lazy-dependency.js'
]

test('rejects a generation missing a lazy chunk and accepts an intact alternate graph', () => {
  const torn = makeBundle(
    completeGraph.filter(file => file !== 'assets/lazy.js'),
    manifest
  )

  const intact = makeBundle(completeGraph, manifest)

  assert.deepEqual(missingRendererAssets(torn), ['assets/lazy.js'])
  assert.deepEqual(missingRendererAssets(intact), [])
})

test('validates imported chunks plus CSS and assets throughout the graph', () => {
  const missing = new Set(['assets/index.css', 'assets/logo.svg', 'assets/lazy.css', 'assets/lazy.woff2'])

  const indexPath = makeBundle(
    completeGraph.filter(file => !missing.has(file)),
    manifest
  )

  assert.deepEqual(new Set(missingRendererAssets(indexPath)), missing)
})

test('falls back to index.html assets when the manifest is absent or unreadable', () => {
  const absent = makeBundle(['assets/index.js'])
  const unreadable = makeBundle(['assets/index.js'])

  fs.writeFileSync(path.join(path.dirname(unreadable), 'manifest.json'), '{not-json')

  assert.deepEqual(missingRendererAssets(absent), ['assets/vendor.js'])
  assert.deepEqual(missingRendererAssets(unreadable), ['assets/vendor.js'])
})

const INDEX_HTML = [
  '<!doctype html>',
  '<html>',
  '  <head>',
  '    <script type="module" crossorigin src="/assets/index-a1b2c3.js"></script>',
  '    <link rel="modulepreload" href="/assets/shiki-block-COiz1pEN.js" />',
  '    <link rel="stylesheet" href="/assets/index-d4e5f6.css" />',
  '  </head>',
  '  <body><div id="root"></div></body>',
  '</html>'
].join('\n')

test('parseModuleAssetRefs collects module scripts and modulepreload hrefs', () => {
  assert.deepEqual(parseModuleAssetRefs(INDEX_HTML), ['assets/index-a1b2c3.js', 'assets/shiki-block-COiz1pEN.js'])
})

test('parseModuleAssetRefs ignores non-module tags (plain stylesheet, non-module script)', () => {
  const html = [
    '<link rel="stylesheet" href="/assets/app.css" />',
    '<script src="/assets/analytics.js"></script>',
    '<script type="module" src="/assets/entry.js"></script>'
  ].join('\n')

  assert.deepEqual(parseModuleAssetRefs(html), ['assets/entry.js'])
})

test('parseModuleAssetRefs drops absolute/CDN URLs — they are not this generation', () => {
  const html = [
    '<script type="module" src="https://cdn.example.com/vendor.js"></script>',
    '<link rel="modulepreload" href="//cdn.example.com/chunk.js" />',
    '<script type="module" src="/assets/local.js"></script>'
  ].join('\n')

  assert.deepEqual(parseModuleAssetRefs(html), ['assets/local.js'])
})

test('parseModuleAssetRefs strips a leading ./ and any query/hash suffix', () => {
  const html = [
    '<script type="module" src="./assets/entry.js?v=123"></script>',
    '<link rel="modulepreload" href="./assets/lazy.js#frag" />'
  ].join('\n')

  assert.deepEqual(parseModuleAssetRefs(html), ['assets/entry.js', 'assets/lazy.js'])
})

test('parseModuleAssetRefs returns [] for empty/nullish/module-free html', () => {
  assert.deepEqual(parseModuleAssetRefs(''), [])
  assert.deepEqual(parseModuleAssetRefs(undefined as unknown as string), [])
  assert.deepEqual(parseModuleAssetRefs('<html><body>no modules here</body></html>'), [])
})

function depsFor(indexDir: string, html: string, presentFiles: string[]): RendererBundleDeps {
  const present = new Set(presentFiles.map(file => path.join(indexDir, file)))

  return {
    readFileSync: () => html,
    existsSync: (file: string) => present.has(file)
  }
}

const INDEX_PATH = path.join('/app', 'dist', 'index.html')
const INDEX_DIR = path.dirname(INDEX_PATH)

test('missingRendererAssets: intact generation reports nothing missing', () => {
  const deps = depsFor(INDEX_DIR, INDEX_HTML, ['assets/index-a1b2c3.js', 'assets/shiki-block-COiz1pEN.js'])

  assert.deepEqual(missingRendererAssets(INDEX_PATH, deps), [])
})

test('missingRendererAssets: torn generation names the dangling chunk', () => {
  const deps = depsFor(INDEX_DIR, INDEX_HTML, ['assets/index-a1b2c3.js'])

  assert.deepEqual(missingRendererAssets(INDEX_PATH, deps), ['assets/shiki-block-COiz1pEN.js'])
})

test('missingRendererAssets: a fully torn copy lists every referenced module', () => {
  const deps = depsFor(INDEX_DIR, INDEX_HTML, [])

  assert.deepEqual(missingRendererAssets(INDEX_PATH, deps), [
    'assets/index-a1b2c3.js',
    'assets/shiki-block-COiz1pEN.js'
  ])
})

test('missingRendererAssets: existence is checked relative to the index dir, per copy', () => {
  const unpackedIndex = path.join('/app', 'app.asar.unpacked', 'dist', 'index.html')
  const unpackedDir = path.dirname(unpackedIndex)
  const intact = depsFor(unpackedDir, INDEX_HTML, ['assets/index-a1b2c3.js', 'assets/shiki-block-COiz1pEN.js'])
  const torn = depsFor(INDEX_DIR, INDEX_HTML, ['assets/index-a1b2c3.js'])

  assert.deepEqual(missingRendererAssets(unpackedIndex, intact), [])
  assert.deepEqual(missingRendererAssets(INDEX_PATH, torn), ['assets/shiki-block-COiz1pEN.js'])
})

test('missingRendererAssets: an unreadable index is not treated as torn', () => {
  const deps: RendererBundleDeps = {
    readFileSync: () => {
      throw new Error('EACCES: permission denied')
    },
    existsSync: () => false
  }

  assert.deepEqual(missingRendererAssets(INDEX_PATH, deps), [])
})

test('missingRendererAssets: an index naming nothing checkable is not torn', () => {
  const deps = depsFor(INDEX_DIR, '<html><body>static shell, no modules</body></html>', [])

  assert.deepEqual(missingRendererAssets(INDEX_PATH, deps), [])
})

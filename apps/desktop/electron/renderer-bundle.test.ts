import assert from 'node:assert/strict'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { afterEach, test } from 'vitest'

import { missingRendererAssets } from './renderer-bundle'

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

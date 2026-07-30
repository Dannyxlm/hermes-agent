import assert from 'node:assert/strict'
import { spawnSync } from 'node:child_process'
import fs from 'node:fs'
import os from 'node:os'
import path from 'node:path'

import { test } from 'vitest'

import { hasGitCheckoutMetadata, resolveGitCheckoutCandidate } from './update-root'

function git(cwd: string, args: string[]): string {
  const result = spawnSync('git', args, { cwd, encoding: 'utf8' })

  assert.equal(result.status, 0, `git ${args.join(' ')} failed: ${result.stderr}`)

  return result.stdout.trim()
}

test('an explicit real Git worktree wins even though its .git metadata is a file', () => {
  const root = fs.mkdtempSync(path.join(os.tmpdir(), 'hermes-update-root-'))
  const repository = path.join(root, 'repository')
  const worktree = path.join(root, 'worktree')

  try {
    fs.mkdirSync(repository)
    git(repository, ['init', '-b', 'main'])
    git(repository, ['config', 'user.email', 'desktop-test@example.invalid'])
    git(repository, ['config', 'user.name', 'Desktop Test'])
    fs.writeFileSync(path.join(repository, 'README.md'), 'test\n')
    git(repository, ['add', 'README.md'])
    git(repository, ['commit', '-m', 'initial'])
    git(repository, ['worktree', 'add', '-b', 'desktop-test', worktree])

    assert.equal(fs.statSync(path.join(worktree, '.git')).isFile(), true)
    assert.equal(hasGitCheckoutMetadata(worktree), true)
    assert.equal(resolveGitCheckoutCandidate([worktree, repository], repository), worktree)
  } finally {
    fs.rmSync(root, { force: true, recursive: true })
  }
})

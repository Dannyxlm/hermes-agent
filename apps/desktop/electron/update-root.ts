import fs from 'node:fs'
import path from 'node:path'

function hasGitCheckoutMetadata(root: string): boolean {
  try {
    const stat = fs.statSync(path.join(root, '.git'))

    return stat.isDirectory() || stat.isFile()
  } catch {
    return false
  }
}

function resolveGitCheckoutCandidate(candidates: readonly (null | string | undefined)[], fallback: string): string {
  const available = candidates.filter((candidate): candidate is string => Boolean(candidate))

  return available.find(hasGitCheckoutMetadata) || available[0] || fallback
}

export { hasGitCheckoutMetadata, resolveGitCheckoutCandidate }

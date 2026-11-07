import type { DesktopVersionInfo } from '@/global'

/** The running package, independent of the updater's mutable runtime checkout. */
export function desktopClientIdentity(version: DesktopVersionInfo | null) {
  return {
    version: version?.clientVersion ?? version?.appVersion,
    sha: version?.commit?.slice(0, 7) ?? null
  }
}

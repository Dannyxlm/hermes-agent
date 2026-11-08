import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { useI18n } from '@/i18n'
import { desktopClientIdentity } from '@/lib/desktop-client-identity'
import { rollbackSentence } from '@/lib/update-copy'
import { resolveVersionStatus } from '@/lib/version-status'
import { $cloudOwnsUpdates, updateTargetFor } from '@/store/cloud-updates'
import { $connection } from '@/store/session'
import { $backendUpdateApply, $backendUpdateStatus, $desktopVersion, $updateApply, $updateStatus } from '@/store/updates'

/**
 * The palette's Update row label: the same install the statusbar names, same
 * target selection, same resolver. Reduced to the label string: an in-flight
 * apply rewrites these stores on every progress line, and only a changed
 * string should rebuild the palette's groups. Null in a Webapp served by
 * Hermes Cloud, where nothing is updatable, so the row is not listed at all.
 */
export function useUpdateRowLabel(): null | string {
  const { t } = useI18n()
  const connection = useStore($connection)
  const desktopVersion = useStore($desktopVersion)
  const clientStatus = useStore($updateStatus)
  const clientApply = useStore($updateApply)
  const backendStatus = useStore($backendUpdateStatus)
  const backendApply = useStore($backendUpdateApply)
  // Desktop connected to Cloud: the row names (and updates) this machine's own app.
  const hidden = useStore($cloudOwnsUpdates).client
  const backend = updateTargetFor(connection) === 'backend'

  return useMemo(() => {
    if (hidden) {
      return null
    }

    const apply = backend ? backendApply : clientApply
    const status = backend ? backendStatus : clientStatus
    // The client names its running package, not the updater's mutable runtime checkout.
    const client = desktopClientIdentity(desktopVersion)

    return resolveVersionStatus({
      applying: apply.applying || apply.stage === 'restart',
      behind: status?.behind ?? 0,
      channel: status?.channel === 'stable' ? 'stable' : 'main',
      copy: t.shell.statusbar,
      latestTag: status?.latestTag,
      remote: backend,
      rollback: rollbackSentence(status, t.settings.about.channel.rollbackBody),
      restarting: apply.stage === 'restart',
      sha: backend ? status?.currentSha?.slice(0, 7) ?? null : client.sha,
      target: backend ? 'backend' : 'client',
      updateAvailable: status?.updateAvailable,
      version: backend ? status?.currentVersion : client.version
    }).label
  }, [backend, backendApply, backendStatus, clientApply, clientStatus, desktopVersion, hidden, t])
}

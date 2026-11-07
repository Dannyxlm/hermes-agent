import { useStore } from '@nanostores/react'
import { useMemo } from 'react'

import { useI18n } from '@/i18n'
import { desktopClientIdentity } from '@/lib/desktop-client-identity'
import { rollbackSentence } from '@/lib/update-copy'
import { resolveVersionStatus } from '@/lib/version-status'
import { $connection } from '@/store/session'
import { $backendUpdateApply, $backendUpdateStatus, $desktopVersion, $updateApply, $updateStatus } from '@/store/updates'

/** Keep the palette's backend selection; local client labels name the package. */
export function useUpdateVersionLabel() {
  const { t } = useI18n()
  const connection = useStore($connection)
  const desktopVersion = useStore($desktopVersion)
  const clientStatus = useStore($updateStatus)
  const clientApply = useStore($updateApply)
  const backendStatus = useStore($backendUpdateStatus)
  const backendApply = useStore($backendUpdateApply)

  return useMemo(() => {
    const backend = connection?.mode === 'remote'
    const apply = backend ? backendApply : clientApply
    const status = backend ? backendStatus : clientStatus
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
  }, [backendApply, backendStatus, clientApply, clientStatus, connection?.mode, desktopVersion, t])
}

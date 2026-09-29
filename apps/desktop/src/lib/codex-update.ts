import type { DesktopUpdateStatus } from '@/global'

const SHA = /^[a-f0-9]{40}$/i

export function usesCodexUpdates(status: DesktopUpdateStatus | null): boolean {
  return Boolean(status?.managedSource || status?.upstreamTracking?.installedRepository === 'Dannyxlm/hermes-agent')
}

/** Only official SHAs enter public compare links; never send a fork identity to upstream. */
export function officialChangesUrl(base?: string | null, target?: string | null): string {
  const repository = 'https://github.com/NousResearch/hermes-agent'

  return base && target && SHA.test(base) && SHA.test(target)
    ? `${repository}/compare/${base}...${target}`
    : `${repository}/releases`
}

export function codexUpdateRequest(status: DesktopUpdateStatus): string {
  const source = status.managedSource
  const tracking = status.upstreamTracking

  const observations = [
    ['Installed Desktop integration', tracking?.installedSha],
    ['Running Ava integration', source?.runningSource],
    ['Recorded official base', source?.runningUpstreamBase],
    ['Observed official head', source?.upstreamHead ?? tracking?.targetSha]
  ].flatMap(([label, value]) => (value && SHA.test(value) ? [`${label}: ${value}`] : []))

  return [
    'Use $ava-hermes-updater to check our live Ava and Hermes Desktop versions, explain the relevant new changes, and prepare their paired update.',
    'Recheck current state before choosing a target; these observations may be stale. Preserve CloudSeed overlays and use the existing release process. Ask before deployment unless it is already authorized in this chat.',
    ...observations
  ].join('\n\n')
}

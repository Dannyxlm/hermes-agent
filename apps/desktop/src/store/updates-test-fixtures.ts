import type { DesktopUpdateStatus } from '@/global'

export const registryOf = (ids: string[]) => ({
  version: 2,
  primary: ids[0] ?? 'local',
  secureTokenStorage: true,
  connections: ids.map(id => ({ id, kind: id === 'local' ? 'local' : 'remote', label: id }))
})

export const status = (over: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  supported: true,
  behind: 3,
  targetSha: 'sha-a',
  fetchedAt: 0,
  ...over
})


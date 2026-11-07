import { backendProfileDescriptor } from './backend-profile-descriptor'
import { profileRemoteOverride } from './connection-config'
import type { DesktopRemoteRoute, DesktopRemoteRouteInput } from './desktop-remote-route'

/** Preserve the namespace selected before dialing, not the later Desktop label. */
export function resolvedRemoteDescriptor<Connection extends object>(
  connection: Connection, route: DesktopRemoteRoute, config: DesktopRemoteRouteInput['config'], profile: string
) {
  const backendProfile = route.kind === 'ssh'
    ? route.ssh.remoteProfile || profile : profileRemoteOverride(config, profile)?.remoteProfile || profile

  return backendProfileDescriptor({ ...connection, ...(route.connectionId ? { connectionId: route.connectionId } : {}) }, backendProfile)
}

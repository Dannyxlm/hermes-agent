/** The dial label and resolver-owned backend namespace must survive independently. */
export function backendProfileDescriptor<Connection extends object>(connection: Connection, backendProfile: string) {
  return { ...connection, backendProfile }
}

/** Shared primary callers retain their requested dial label; pooled descriptors also carry
 * profile, so only this wrapper marks sharedPrimary. Local bindings already prove their key. */
export function primaryRouteDescriptor<Connection extends object>(
  connection: Connection, key: string, route: { descriptorProfile: null | string }
) {
  const scoped = 'mode' in connection && connection.mode === 'local' && !('backendProfile' in connection)
    ? backendProfileDescriptor(connection, key) : connection

  return route.descriptorProfile
    ? { ...scoped, profile: route.descriptorProfile, sharedPrimary: true }
    : { ...scoped, profile: key }
}

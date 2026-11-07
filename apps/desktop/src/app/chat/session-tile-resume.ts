import { patchSessionTile, type SessionTileDelegate } from '@/store/session-states'

/** Publish the binding only after the real delegate has hydrated the stored transcript. */
export async function resumeAndBindSessionTile(storedSessionId: string, delegate: Pick<SessionTileDelegate, 'resumeTile'>) {
  const runtimeId = await delegate.resumeTile(storedSessionId)
  patchSessionTile(storedSessionId, { error: undefined, runtimeId })

  return runtimeId
}

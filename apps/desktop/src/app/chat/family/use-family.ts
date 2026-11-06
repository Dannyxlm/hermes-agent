import { useEffect, useState } from 'react'

import { type FamilyTarget, requestFamily } from './actions'
import { type FamilyView, parseFamily } from './model'

export type FamilyRead =
  | { kind: 'loading' }
  | { kind: 'ready'; view: FamilyView }
  | { kind: 'unavailable' }
  | { kind: 'error'; message: string }

export function useFamily(target: FamilyTarget, overview: boolean, revision: number): FamilyRead {
  const key = JSON.stringify([target, overview, revision])
  const [snapshot, setSnapshot] = useState<{ key: string; read: FamilyRead } | null>(null)
  useEffect(() => {
    let cancelled = false
    let timer: ReturnType<typeof setTimeout> | undefined

    const publish = (read: FamilyRead) => { if (!cancelled) { setSnapshot({ key, read }) } }

    const schedule = () => {
      timer = setTimeout(() => {
        if (document.visibilityState === 'hidden') { schedule() } else { void refresh() }
      }, 10_000)
    }

    const refresh = async () => {
      try {
        const view = parseFamily(await requestFamily(target, overview), overview)
        publish({ kind: 'ready', view })
      } catch (error) {
        if (cancelled) { return }
        const message = error instanceof Error ? error.message : String(error)
        const unsupported = /method not found|unknown method|unsupported.*family/i.test(message)
        publish(unsupported ? { kind: 'unavailable' } : { kind: 'error', message })

        return
      }

      if (!cancelled) { schedule() }
    }

    void refresh()

    return () => { cancelled = true; clearTimeout(timer) }
  }, [target, overview, revision, key])

  return snapshot?.key === key ? snapshot.read : { kind: 'loading' }
}

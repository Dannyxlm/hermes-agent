import { describe, expect, it, vi } from 'vitest'

import { hostedMessage, HostedRoomsController, hostedWriteIssue } from './hosted-rooms'
import type {
  HostedCapabilities,
  HostedEvent,
  HostedLogPage,
  HostedRoom,
  HostedRoomsDependencies,
  HostedSendOperation,
  HostedTask
} from './hosted-rooms'

const room: HostedRoom = {
  room_id: 'room-test',
  name: 'Review',
  members: [
    { member_id: 'iris', profile: 'iris', handle: 'iris' },
    { member_id: 'vox', profile: 'vox', handle: 'vox' }
  ],
  authority_gateway_id: 'install:test',
  authority_epoch: 1
}

const cap: HostedCapabilities = {
  protocol_version: 2,
  authority_gateway_id: 'install:test',
  features: ['room_identity', 'authority_epoch', 'idempotent_send', 'monotonic_log', 'exact_task_stop'],
  methods: ['groups.list', 'groups.state', 'groups.log', 'groups.send', 'groups.stop'],
  driver: true,
  persistent_process: true,
  max_log_limit: 500
}

const task: HostedTask = { task_id: 'task-current', execution_generation: 2, cancel_generation: 0, status: 'running' }

function harness() {
  const storage = new Map<string, unknown>()
  const events: HostedEvent[] = []
  let capabilities = structuredClone(cap)
  let currentRoom = structuredClone(room)
  let currentTasks = [task]
  let isCurrent = true
  let unknown = false
  let eventCounter = 0
  const accepted = new Map<string, HostedEvent>()

  const request = vi.fn(async (method: string, params: Record<string, unknown> = {}): Promise<unknown> => {
    if (method === 'groups.capabilities') {
      return capabilities
    }

    if (method === 'groups.list') {
      return { rooms: [currentRoom], next_offset: null }
    }

    if (method === 'groups.state') {
      return { room: currentRoom, driver_status: { working: true, stoppable_tasks: currentTasks } }
    }

    if (method === 'groups.log') {
      const pageEvents = events.filter(event => event.seq > Number(params.since_seq)).slice(0, Number(params.limit))
      const cursor = pageEvents.at(-1)?.seq ?? Number(params.since_seq)

      return {
        events: pageEvents,
        cursor,
        latest_seq: events.length,
        has_more: cursor < events.length,
        authority: { gateway_id: currentRoom.authority_gateway_id, epoch: currentRoom.authority_epoch }
      } satisfies HostedLogPage
    }

    if (method === 'groups.send') {
      const existing = accepted.get(String(params.event_id))

      const event = existing ?? {
        room_id: String(params.room_id),
        event_id: `server-${params.event_id}`,
        seq: events.length + 1,
        kind: 'message.user',
        actor: { kind: 'user', id: 'desktop' },
        payload: structuredClone(params.payload) as Record<string, unknown>,
        authority_epoch: currentRoom.authority_epoch,
        created_at: 1000
      }

      if (!existing) {
        accepted.set(String(params.event_id), event)
        events.push(event)
      }

      if (unknown) {
        throw new Error('Socket closed after acceptance')
      }

      return { accepted: true, client_event_id: params.event_id, event }
    }

    if (method === 'groups.stop') {
      return {
        cancelled: 1,
        stop_requested: true,
        task: {
          ...task,
          execution_generation: params.expected_execution_generation,
          cancel_generation: Number(params.expected_cancel_generation) + 1,
          status: 'cancelled'
        }
      }
    }

    throw new Error(`Unexpected RPC: ${method}`)
  })

  const deps: HostedRoomsDependencies = {
    scope: JSON.stringify(['connection-a', 'default']),
    uuid: () => `event-${++eventCounter}`,
    isCurrent: () => isCurrent,
    request: async <T>(method: string, params?: Record<string, unknown>) => (await request(method, params)) as T,
    storage: {
      get: <T>(key: string, fallback: T): T => (storage.has(key) ? (structuredClone(storage.get(key)) as T) : fallback),
      set: (key, value) => {
        storage.set(key, structuredClone(value))
      },
      remove: key => {
        storage.delete(key)
      }
    }
  }

  return {
    controller: new HostedRoomsController(deps),
    deps,
    request,
    events,
    storage,
    setCapabilities: (value: HostedCapabilities) => {
      capabilities = value
    },
    setRoom: (value: HostedRoom) => {
      currentRoom = value
    },
    setTasks: (value: HostedTask[]) => {
      currentTasks = value
    },
    setCurrent: (value: boolean) => {
      isCurrent = value
    },
    setUnknown: (value: boolean) => {
      unknown = value
    }
  }
}

describe('hosted-room sends', () => {
  it('persists the immutable operation before dispatch and reuses it after a lost acknowledgement and reload', async () => {
    const h = harness()
    await h.controller.open(room)
    const request = h.deps.request

    h.deps.request = async <T>(method: string, params?: Record<string, unknown>): Promise<T> => {
      if (method === 'groups.send') {
        expect([...h.storage.values()]).toEqual([
          expect.objectContaining({ room_id: params?.room_id, event_id: params?.event_id, payload: params?.payload })
        ])
      }

      return request<T>(method, params)
    }

    h.setUnknown(true)
    expect(await h.controller.send('Review the draft', ['iris'])).toBe(false)
    expect(h.events).toHaveLength(1)
    const first = h.request.mock.calls.find(([method]) => method === 'groups.send')![1]
    expect(first).toEqual({
      room_id: room.room_id,
      event_id: expect.any(String),
      payload: { text: '@iris Review the draft', thread_id: expect.any(String) }
    })
    expect(h.storage.size).toBe(1)
    h.controller.dispose()
    const reopened = new HostedRoomsController(h.deps)
    await reopened.open(room)
    expect(reopened.state.get().issue).toBe('unknownDelivery')
    expect(h.request.mock.calls.filter(([method]) => method === 'groups.send')).toHaveLength(1)
    expect(await reopened.send('Changed draft', ['vox'])).toBe(false)
    h.setUnknown(false)
    expect(await reopened.retry()).toBe(true)
    expect(h.request.mock.calls.filter(([method]) => method === 'groups.send').map(([, params]) => params)).toEqual([
      first,
      first
    ])
    expect(h.events).toHaveLength(1)
    expect(reopened.state.get().events).toHaveLength(1)
    expect(h.storage.size).toBe(0)
  })

  it('preserves a successor receipt when the original controller acknowledges after disposal and exact retry', async () => {
    const h = harness()
    await h.controller.open(room)
    const request = h.deps.request
    let acknowledgeOriginal!: () => void

    const originalAcknowledgment = new Promise<void>(resolve => {
      acknowledgeOriginal = resolve
    })

    let originalAccepted!: () => void

    const originalAcceptance = new Promise<void>(resolve => {
      originalAccepted = resolve
    })

    let firstSend = true

    h.deps.request = async <T>(method: string, params?: Record<string, unknown>): Promise<T> => {
      const result = await request<T>(method, params)

      if (method === 'groups.send' && firstSend) {
        firstSend = false
        originalAccepted()
        await originalAcknowledgment
      }

      return result
    }

    const originalSend = h.controller.send('Original', ['iris'])
    await originalAcceptance
    const original = h.controller.state.get().pending!
    h.controller.dispose()
    const reopened = new HostedRoomsController(h.deps)
    await reopened.open(room)
    expect(reopened.state.get().pending).toEqual(original)
    expect(await reopened.retry()).toBe(true)
    h.setUnknown(true)
    expect(await reopened.send('Successor', ['vox'])).toBe(false)
    const successor = reopened.state.get().pending!
    expect(successor.event_id).not.toBe(original.event_id)

    acknowledgeOriginal()
    const originalResult = await originalSend

    expect([...h.storage.values()]).toEqual([successor])
    expect(originalResult).toBe(false)
    reopened.dispose()
    const restored = new HostedRoomsController(h.deps)
    await restored.open(room)
    expect(restored.state.get().pending).toEqual(successor)
    h.setUnknown(false)
    expect(await restored.retry()).toBe(true)
    const sends = h.request.mock.calls.filter(([method]) => method === 'groups.send').map(([, params]) => params)
    expect(sends[0]).toEqual(sends[1])
    expect(sends[2]).toEqual(sends[3])
    expect(h.events).toHaveLength(2)
  })

  it('keeps deferred receipt removal ahead of successor persistence across controllers', async () => {
    const h = harness()
    await h.controller.open(room)
    const successorController = new HostedRoomsController(h.deps)
    await successorController.open(room)
    const remove = h.deps.storage.remove
    let finishRemoval!: () => void

    const removalAllowed = new Promise<void>(resolve => {
      finishRemoval = resolve
    })

    let removalStarted!: () => void

    const removing = new Promise<void>(resolve => {
      removalStarted = resolve
    })

    let deferFirstRemoval = true

    h.deps.storage.remove = async key => {
      if (deferFirstRemoval) {
        deferFirstRemoval = false
        removalStarted()
        await removalAllowed
      }

      await remove(key)
    }

    const originalSend = h.controller.send('Original', ['iris'])
    await removing
    h.controller.dispose()
    h.setUnknown(true)
    // This view was already open with an empty receipt. Its new persistence must
    // wait until the old receipt's in-progress removal has finished.
    const successorSend = successorController.send('Successor', ['vox'])
    finishRemoval()
    const originalResult = await originalSend
    expect(await successorSend).toBe(false)
    const successor = successorController.state.get().pending!
    expect(successor).not.toBeNull()
    expect([...h.storage.values()]).toEqual([successor])
    expect(originalResult).toBe(false)

    // Even an active controller may only clear the exact receipt it acknowledged.
    const key = [...h.storage.keys()][0]
    h.setUnknown(false)

    for (const changedIdentity of [
      { event_id: 'replacement-event' },
      { scope: 'other-source' },
      { room_id: 'other-room' }
    ]) {
      const replacement = { ...successor, ...changedIdentity } satisfies HostedSendOperation
      h.storage.set(key, replacement)
      expect(await successorController.retry()).toBe(false)
      expect([...h.storage.values()]).toEqual([replacement])
    }
  })

  it('fails closed on old protocol, missing idempotency, nonpersistent workers, invalid recipients and storage failure', async () => {
    for (const override of [
      { protocol_version: 1 },
      { features: ['room_identity'] },
      { driver: false },
      { persistent_process: false }
    ]) {
      const h = harness()
      h.setCapabilities({ ...cap, ...override })
      await h.controller.open(room)
      expect(await h.controller.send('Hello', ['iris'])).toBe(false)
      expect(h.request.mock.calls.some(([method]) => method === 'groups.send')).toBe(false)
    }

    const h = harness()
    await h.controller.open(room)
    expect(() => hostedMessage(room, 'hello', ['removed'], 'thread')).toThrow('invalidRecipients')
    expect(() => hostedMessage(room, '@vox please help', ['iris'], 'thread')).toThrow('invalidRecipients')
    expect(() => hostedMessage(room, '@all please help', ['iris'], 'thread')).toThrow('invalidRecipients')
    h.deps.storage.set = () => undefined
    expect(await h.controller.send('Hello', ['iris'])).toBe(false)
    expect(h.controller.state.get().issue).toBe('storageUnavailable')
    expect(h.request.mock.calls.some(([method]) => method === 'groups.send')).toBe(false)

    const corrupted = harness()
    corrupted.deps.storage.get = <T>() => ({ room_id: 'wrong-room' }) as T
    await corrupted.controller.open(room)
    await corrupted.controller.refresh()
    expect(hostedWriteIssue(corrupted.controller.state.get())).toBe('storageUnavailable')
    expect(await corrupted.controller.send('Hello', ['iris'])).toBe(false)
    expect(corrupted.request.mock.calls.some(([method]) => method === 'groups.send')).toBe(false)
  })
})

describe('hosted-room replay and scope', () => {
  it('requests bounded room-list pages and retains already loaded rows when refreshing the first page', async () => {
    const h = harness()
    const original = h.deps.request

    h.deps.request = async <T>(method: string, params?: Record<string, unknown>): Promise<T> => {
      if (method !== 'groups.list') {
        return original<T>(method, params)
      }

      await h.request(method, params)
      expect(params?.limit).toBe(50)
      const second = params?.offset === 50

      return {
        rooms: [{ ...room, room_id: second ? 'second-room' : room.room_id }],
        next_offset: second ? null : 50
      } as T
    }

    await h.controller.list()
    await h.controller.list(true)
    await h.controller.list()
    expect(h.controller.state.get().rooms.map(row => row.room_id)).toEqual([room.room_id, 'second-room'])
    expect(
      h.request.mock.calls.filter(([method]) => method === 'groups.list').map(([, params]) => params?.offset)
    ).toEqual([0, 50, 0])
  })
  it('pages a bounded ordered log, rejects mixed-authority pages, and resets on a verified authority change', async () => {
    const h = harness()

    for (let seq = 1; seq <= 53; seq++) {
      h.events.push({
        room_id: room.room_id,
        seq,
        event_id: `event-${seq}`,
        kind: 'message.member',
        actor: { kind: 'member', id: 'iris' },
        authority_epoch: 1,
        payload: { text: `Reply ${seq}` },
        created_at: 1000
      })
    }

    await h.controller.open(room)
    expect(h.controller.state.get().events).toHaveLength(50)
    expect(h.controller.state.get().hasMore).toBe(true)
    await h.controller.refresh()
    expect(h.controller.state.get().events.map(event => event.seq)).toEqual(Array.from({ length: 53 }, (_, i) => i + 1))
    expect(
      h.request.mock.calls.filter(([method]) => method === 'groups.log').map(([, params]) => params?.since_seq)
    ).toEqual([0, 50])
    const original = h.deps.request

    h.deps.request = async <T>(method: string, params?: Record<string, unknown>): Promise<T> => {
      const value = await original<T>(method, params)

      return method === 'groups.log' ? { ...value, authority: { gateway_id: 'install:other', epoch: 2 } } : value
    }

    await h.controller.refresh()
    expect(h.controller.state.get().issue).toBe('authorityChanged')
    expect(hostedWriteIssue(h.controller.state.get())).toBe('unavailable')
    h.deps.request = original
    h.setRoom({ ...room, authority_epoch: 2 })
    await h.controller.refresh()
    expect(h.controller.state.get().cursor).toBe(50)
    expect(h.controller.state.get().authority?.epoch).toBe(2)
    expect(h.controller.state.get().events).toHaveLength(50)
  })

  it('does not publish an old source response or dispatch a deferred send after the source changes', async () => {
    const h = harness()
    let resolve!: (value: unknown) => void
    const original = h.deps.request
    h.deps.request = <T>() =>
      new Promise<T>(done => {
        resolve = done as typeof resolve
      })
    const opening = h.controller.open(room)
    await Promise.resolve()
    await Promise.resolve()
    h.setCurrent(false)
    resolve(cap)
    await opening
    expect(h.controller.state.get().fresh).toBe(false)
    expect(h.request).not.toHaveBeenCalled()
    h.setCurrent(true)
    h.deps.request = original
    await h.controller.open(room)
    const set = h.deps.storage.set

    h.deps.storage.set = async (key, value) => {
      await set(key, value)
      h.setCurrent(false)
    }

    await h.controller.send('Stay on this source', ['iris'])
    expect(h.request.mock.calls.some(([method]) => method === 'groups.send')).toBe(false)
    const other = new HostedRoomsController({ ...h.deps, scope: 'connection-b', isCurrent: () => true })
    await other.open(room)
    expect(other.state.get().pending).toBeNull()
  })
})

describe('exact task stop', () => {
  it('sends all observed task and authority fences, and refuses stale or unsupported task controls', async () => {
    const h = harness()
    await h.controller.open(room)
    expect(await h.controller.stop(task)).toBe(true)
    expect(h.request).toHaveBeenCalledWith('groups.stop', {
      room_id: room.room_id,
      cancel_id: expect.any(String),
      expected_task_id: task.task_id,
      expected_execution_generation: 2,
      expected_cancel_generation: 0,
      expected_authority_gateway_id: room.authority_gateway_id,
      expected_authority_epoch: 1
    })
    h.setTasks([{ ...task, execution_generation: 3 }])
    await h.controller.refresh()
    expect(await h.controller.stop(task)).toBe(false)
    h.setCapabilities({ ...cap, features: cap.features.filter(feature => feature !== 'exact_task_stop') })
    await h.controller.refresh()
    expect(await h.controller.stop({ ...task, execution_generation: 3 })).toBe(false)
    expect(h.request.mock.calls.filter(([method]) => method === 'groups.stop')).toHaveLength(1)
  })

  it('retains disbanded history and disables further writes', async () => {
    const h = harness()
    h.setRoom({ ...room, disbanded_at: 123 })
    await h.controller.open(room)
    expect(hostedWriteIssue(h.controller.state.get())).toBe('disbanded')
    expect(await h.controller.send('Hello', ['iris'])).toBe(false)
    expect(await h.controller.stop(task)).toBe(false)
  })
})

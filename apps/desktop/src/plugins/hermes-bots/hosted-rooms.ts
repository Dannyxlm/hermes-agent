/** Server-owned rooms only. This cache never reads the local group-chat store. */
import { atom } from 'nanostores'

export interface HostedMember {
  member_id: string
  profile: string
  handle: string
  display_name?: string
}

export interface HostedRoom {
  room_id: string
  name: string
  members: HostedMember[]
  authority_gateway_id: string
  authority_epoch: number
  latest_seq?: number
  disbanded_at?: number
}

export interface HostedEvent {
  room_id: string
  seq: number
  event_id: string
  kind: string
  actor: { kind: string; id: string }
  authority_epoch: number
  payload: Record<string, unknown>
  created_at: number
}

export interface HostedAuthority {
  gateway_id: string
  epoch: number
}

export interface HostedCapabilities {
  protocol_version: number
  authority_gateway_id: string
  features: string[]
  methods: string[]
  driver: boolean
  persistent_process: boolean
  max_log_limit: number
}

export interface HostedTask {
  task_id: string
  execution_generation: number
  cancel_generation: number
  status: string
  member_id?: string
}

export interface HostedDriverStatus {
  working?: boolean
  blocked?: boolean
  pending_actions?: { kind: string; task_id?: string; member_id?: string }[]
  stoppable_tasks?: HostedTask[]
}

export interface HostedLogPage {
  events: HostedEvent[]
  authority: HostedAuthority
  cursor: number
  latest_seq: number
  has_more: boolean
}

export interface HostedSendOperation {
  scope: string
  authority: HostedAuthority
  room_id: string
  event_id: string
  payload: { text: string; thread_id: string }
}

export type HostedIssue =
  | 'unavailable'
  | 'unsupported'
  | 'driverUnavailable'
  | 'authorityChanged'
  | 'invalidLog'
  | 'disbanded'
  | 'storageUnavailable'
  | 'unknownDelivery'
  | 'invalidRecipients'
  | 'emptyMessage'
  | 'staleTask'

export interface HostedRoomsState {
  rooms: HostedRoom[]
  nextOffset: number | null
  capabilities: HostedCapabilities | null
  selected: HostedRoom | null
  driver: HostedDriverStatus | null
  events: HostedEvent[]
  authority: HostedAuthority | null
  cursor: number
  hasMore: boolean
  pending: HostedSendOperation | null
  receiptReady: boolean
  loading: boolean
  writing: boolean
  fresh: boolean
  issue: HostedIssue | null
}

export interface HostedRoomsDependencies {
  scope: string
  request<T>(method: string, params?: Record<string, unknown>): Promise<T>
  isCurrent(): boolean
  storage: {
    get<T>(key: string, fallback: T): T | Promise<T>
    set(key: string, value: unknown): void | Promise<void>
    remove(key: string): void | Promise<void>
  }
  uuid(): string
}

const REQUIRED_FEATURES = ['room_identity', 'idempotent_send', 'monotonic_log', 'authority_epoch']
const READ_METHODS = ['groups.list', 'groups.state', 'groups.log']
const PAGE_SIZE = 50

// Controllers for the same source/room share a receipt. Keep its check/write or
// check/remove sequence indivisible even when a storage adapter defers an operation.
const receiptMutations = new Map<string, Promise<void>>()

async function mutateReceipt<T>(key: string, action: () => Promise<T>): Promise<T> {
  const previous = receiptMutations.get(key) ?? Promise.resolve()
  let release!: () => void

  const pending = new Promise<void>(resolve => {
    release = resolve
  })

  receiptMutations.set(key, pending)
  await previous

  try {
    return await action()
  } finally {
    release()

    if (receiptMutations.get(key) === pending) {
      receiptMutations.delete(key)
    }
  }
}

export function hostedReadSupported(cap: HostedCapabilities | null): boolean {
  return Boolean(
    cap &&
    cap.protocol_version === 2 &&
    Array.isArray(cap.features) &&
    Array.isArray(cap.methods) &&
    REQUIRED_FEATURES.every(feature => cap.features.includes(feature)) &&
    READ_METHODS.every(method => cap.methods.includes(method)) &&
    Number.isInteger(cap.max_log_limit) &&
    cap.max_log_limit > 0
  )
}

export function hostedWriteIssue(
  state: HostedRoomsState,
  method: 'groups.send' | 'groups.stop' = 'groups.send'
): HostedIssue | null {
  const cap = state.capabilities

  if (!hostedReadSupported(cap) || !cap?.methods.includes(method)) {
    return 'unsupported'
  }

  if (cap.driver !== true || cap.persistent_process !== true) {
    return 'driverUnavailable'
  }

  if (method === 'groups.send' && !state.receiptReady) {
    return 'storageUnavailable'
  }

  if (!state.fresh || !state.selected) {
    return 'unavailable'
  }

  if (state.selected.disbanded_at != null) {
    return 'disbanded'
  }

  if (cap.authority_gateway_id !== state.selected.authority_gateway_id) {
    return 'authorityChanged'
  }

  return null
}

function sameAuthority(a: HostedAuthority | null, b: HostedAuthority): boolean {
  return a?.gateway_id === b.gateway_id && a.epoch === b.epoch
}

function roomAuthority(room: HostedRoom): HostedAuthority {
  return { gateway_id: room.authority_gateway_id, epoch: room.authority_epoch }
}

function validRoom(room: HostedRoom): boolean {
  return Boolean(
    room &&
    typeof room.room_id === 'string' &&
    room.room_id &&
    typeof room.name === 'string' &&
    typeof room.authority_gateway_id === 'string' &&
    room.authority_gateway_id &&
    Number.isInteger(room.authority_epoch) &&
    room.authority_epoch > 0 &&
    Array.isArray(room.members) &&
    room.members.every(
      member =>
        typeof member.member_id === 'string' &&
        member.member_id &&
        typeof member.profile === 'string' &&
        member.profile &&
        typeof member.handle === 'string' &&
        /^[A-Za-z0-9][A-Za-z0-9._:-]*$/.test(member.handle)
    ) &&
    new Set(room.members.map(member => member.member_id)).size === room.members.length &&
    new Set(room.members.map(member => member.handle.toLowerCase())).size === room.members.length
  )
}

/** Refuse gaps, cursor regressions and mixed authorities instead of painting a false history. */
export function appendHostedPage(state: HostedRoomsState, page: HostedLogPage): Partial<HostedRoomsState> {
  if (
    !state.selected ||
    !sameAuthority(roomAuthority(state.selected), page.authority) ||
    (state.authority && !sameAuthority(state.authority, page.authority))
  ) {
    throw new Error('authorityChanged')
  }

  let cursor = state.cursor

  for (const event of page.events) {
    if (
      event.room_id !== state.selected.room_id ||
      event.seq !== cursor + 1 ||
      !Number.isInteger(event.authority_epoch) ||
      event.authority_epoch < 1 ||
      event.authority_epoch > page.authority.epoch ||
      !Number.isFinite(event.created_at) ||
      !Number.isFinite(new Date(event.created_at * 1000).getTime()) ||
      typeof event.actor?.id !== 'string' ||
      typeof event.kind !== 'string' ||
      typeof event.event_id !== 'string' ||
      !event.payload ||
      typeof event.payload !== 'object'
    ) {
      throw new Error('invalidLog')
    }

    cursor = event.seq
  }

  if (
    page.cursor !== cursor ||
    !Number.isInteger(page.latest_seq) ||
    page.latest_seq < cursor ||
    page.has_more !== cursor < page.latest_seq ||
    (page.has_more && cursor === state.cursor)
  ) {
    throw new Error('invalidLog')
  }

  return {
    events: page.events.length ? [...state.events, ...page.events] : state.events,
    cursor,
    hasMore: page.has_more,
    authority: page.authority
  }
}

/** The wire has no recipients field: the Discussion driver resolves member handles in text. */
export function hostedMessage(room: HostedRoom, text: string, recipientIds: string[], threadId: string) {
  const members = room.members.filter(member => recipientIds.includes(member.member_id))

  if (!members.length || new Set(recipientIds).size !== recipientIds.length || members.length !== recipientIds.length) {
    throw new Error('invalidRecipients')
  }

  const selected = new Set(members.map(member => member.handle.toLowerCase()))
  const all = members.length === room.members.length

  for (const match of text.matchAll(/@([A-Za-z0-9][A-Za-z0-9._:-]*)/g)) {
    const handle = match[1].toLowerCase()

    if (
      !all &&
      (['all', 'everyone'].includes(handle) ||
        room.members.some(member => member.handle.toLowerCase() === handle && !selected.has(handle)))
    ) {
      throw new Error('invalidRecipients')
    }
  }

  if (!text.trim()) {
    throw new Error('emptyMessage')
  }

  const prefix = all ? '@all' : members.map(member => `@${member.handle}`).join(' ')
  const payload = { text: `${prefix} ${text.trim()}`, thread_id: threadId }

  if (new TextEncoder().encode(payload.text).length > 64 * 1024) {
    throw new Error('emptyMessage')
  }

  return payload
}

function initialState(): HostedRoomsState {
  return {
    rooms: [],
    nextOffset: null,
    capabilities: null,
    selected: null,
    driver: null,
    events: [],
    authority: null,
    cursor: 0,
    hasMore: false,
    pending: null,
    receiptReady: false,
    loading: false,
    writing: false,
    fresh: false,
    issue: null
  }
}

export class HostedRoomsController {
  readonly state = atom<HostedRoomsState>(initialState())
  private generation = 0
  private disposed = false
  constructor(private readonly deps: HostedRoomsDependencies) {}

  activate() {
    this.disposed = false
    this.patch({ loading: false, writing: false, fresh: false })
  }
  dispose() {
    this.disposed = true
    this.generation++
  }
  private current(generation = this.generation) {
    return !this.disposed && generation === this.generation && this.deps.isCurrent()
  }
  private patch(value: Partial<HostedRoomsState>) {
    this.state.set({ ...this.state.get(), ...value })
  }
  private key(roomId: string) {
    return `hosted-room-send-v1:${JSON.stringify([this.deps.scope, roomId])}`
  }
  private async request<T>(method: string, params: Record<string, unknown> = {}): Promise<T> {
    if (!this.current()) {
      throw new Error('unavailable')
    }

    return this.deps.request<T>(method, params)
  }

  async list(more = false) {
    if (!this.current() || this.state.get().loading || this.state.get().writing) {
      return
    }

    const generation = ++this.generation
    const offset = more ? this.state.get().nextOffset : 0

    if (offset === null) {
      return
    }

    this.patch({ loading: true, issue: null })

    try {
      const capabilities = await this.request<HostedCapabilities>('groups.capabilities')

      if (!this.current(generation)) {
        return
      }

      this.patch({ capabilities })

      if (!hostedReadSupported(capabilities)) {
        throw new Error('unsupported')
      }

      const result = await this.request<{ rooms: HostedRoom[]; next_offset: number | null }>('groups.list', {
        limit: PAGE_SIZE,
        offset
      })

      if (!this.current(generation)) {
        return
      }

      if (
        !Array.isArray(result.rooms) ||
        result.rooms.length > PAGE_SIZE ||
        !result.rooms.every(validRoom) ||
        (result.next_offset !== null && (!Number.isInteger(result.next_offset) || result.next_offset <= offset))
      ) {
        throw new Error('invalidLog')
      }

      const rooms = new Map(this.state.get().rooms.map(room => [room.room_id, room]))

      for (const room of result.rooms) {
        rooms.set(room.room_id, room)
      }

      this.patch({ rooms: [...rooms.values()], nextOffset: result.next_offset })
    } catch (error) {
      if (this.current(generation)) {
        this.patch({
          issue: error instanceof Error && error.message === 'unsupported' ? 'unsupported' : 'unavailable',
          fresh: false
        })
      }
    } finally {
      if (this.current(generation)) {
        this.patch({ loading: false })
      }
    }
  }

  async open(room: HostedRoom) {
    if (!this.current() || this.state.get().writing) {
      return
    }

    if (!validRoom(room)) {
      this.patch({ issue: 'invalidLog', fresh: false })

      return
    }

    this.generation++
    this.patch({
      selected: room,
      events: [],
      cursor: 0,
      authority: null,
      hasMore: false,
      driver: null,
      pending: null,
      receiptReady: false,
      loading: false,
      fresh: false,
      issue: null
    })
    const generation = this.generation

    try {
      const pending = await this.deps.storage.get<HostedSendOperation | null>(this.key(room.room_id), null)

      if (!this.current(generation)) {
        return
      }

      if (
        pending &&
        (pending.scope !== this.deps.scope ||
          pending.room_id !== room.room_id ||
          typeof pending.event_id !== 'string' ||
          !pending.event_id ||
          typeof pending.payload?.text !== 'string' ||
          typeof pending.payload?.thread_id !== 'string' ||
          !pending.authority?.gateway_id ||
          !Number.isInteger(pending.authority.epoch))
      ) {
        throw new Error('storageUnavailable')
      }

      this.patch({ pending, receiptReady: true, issue: pending ? 'unknownDelivery' : null })
    } catch {
      if (this.current(generation)) {
        this.patch({ issue: 'storageUnavailable' })
      }

      return
    }

    await this.refresh()
  }

  async refresh(reload = false) {
    const current = this.state.get()

    if (!this.current() || !current.selected || current.loading || current.writing) {
      return
    }

    const generation = this.generation
    this.patch({
      loading: true,
      ...(reload ? { events: [], cursor: 0, authority: null, hasMore: false, fresh: false } : {})
    })

    try {
      const capabilities = await this.request<HostedCapabilities>('groups.capabilities')

      if (!this.current(generation)) {
        return
      }

      this.patch({ capabilities })

      if (!hostedReadSupported(capabilities)) {
        throw new Error('unsupported')
      }

      const result = await this.request<{ room: HostedRoom; driver_status?: HostedDriverStatus }>('groups.state', {
        room_id: current.selected.room_id,
        include_disbanded: true
      })

      if (!this.current(generation)) {
        return
      }

      if (!validRoom(result.room) || result.room.room_id !== current.selected.room_id) {
        throw new Error('invalidLog')
      }

      const changed = !sameAuthority(roomAuthority(current.selected), roomAuthority(result.room))
      this.patch({
        selected: result.room,
        driver: result.driver_status ?? null,
        fresh: false,
        ...(changed ? { events: [], authority: null, cursor: 0, hasMore: false } : {})
      })
      const snapshot = this.state.get()

      const page = await this.request<HostedLogPage>('groups.log', {
        room_id: result.room.room_id,
        since_seq: snapshot.cursor,
        limit: Math.min(PAGE_SIZE, capabilities.max_log_limit),
        include_disbanded: true
      })

      if (!this.current(generation)) {
        return
      }

      this.patch({
        ...appendHostedPage(snapshot, page),
        fresh: true,
        issue: snapshot.pending ? 'unknownDelivery' : changed ? 'authorityChanged' : null
      })
    } catch (error) {
      const issue =
        error instanceof Error && ['authorityChanged', 'invalidLog', 'unsupported'].includes(error.message)
          ? (error.message as HostedIssue)
          : 'unavailable'

      if (this.current(generation)) {
        this.patch({ issue, fresh: false })
      }
    } finally {
      if (this.current(generation)) {
        this.patch({ loading: false })
      }
    }
  }

  async send(text: string, recipientIds: string[]): Promise<boolean> {
    const state = this.state.get()

    if (!this.current() || state.writing || state.loading || state.pending) {
      return false
    }

    const issue = hostedWriteIssue(state)

    if (issue) {
      this.patch({ issue })

      return false
    }

    let operation: HostedSendOperation

    try {
      operation = {
        scope: this.deps.scope,
        room_id: state.selected!.room_id,
        event_id: this.deps.uuid(),
        authority: roomAuthority(state.selected!),
        payload: hostedMessage(state.selected!, text, recipientIds, this.deps.uuid())
      }
    } catch (error) {
      this.patch({ issue: (error as Error).message as HostedIssue })

      return false
    }

    this.patch({ writing: true })
    const generation = this.generation

    try {
      const key = this.key(operation.room_id)

      const persisted = await mutateReceipt(key, async () => {
        if (!this.current(generation)) {
          return false
        }

        const existing = await this.deps.storage.get(key, null)

        if (!this.current(generation)) {
          return false
        }

        if (existing !== null) {
          throw new Error('storageUnavailable')
        }

        // Plugin storage is best-effort; readback is required before any dispatch.
        await this.deps.storage.set(key, operation)
        const stored = await this.deps.storage.get(key, null)

        if (JSON.stringify(stored) !== JSON.stringify(operation)) {
          throw new Error('storageUnavailable')
        }

        return true
      })

      if (!persisted) {
        return false
      }
    } catch {
      if (this.current(generation)) {
        this.patch({ writing: false, issue: 'storageUnavailable' })
      }

      return false
    }

    if (!this.current(generation)) {
      return false
    }

    this.patch({ pending: operation })

    return this.dispatch(operation)
  }

  async retry(): Promise<boolean> {
    const state = this.state.get()

    if (!this.current() || state.writing || state.loading || !state.pending) {
      return false
    }

    const issue = hostedWriteIssue(state)

    if (issue) {
      this.patch({ issue })

      return false
    }

    if (!sameAuthority(state.pending.authority, roomAuthority(state.selected!))) {
      this.patch({ issue: 'authorityChanged' })

      return false
    }

    this.patch({ writing: true })

    return this.dispatch(state.pending)
  }

  private async dispatch(operation: HostedSendOperation): Promise<boolean> {
    const generation = this.generation

    try {
      const result = await this.request<{ accepted: boolean; client_event_id: string; event: HostedEvent }>(
        'groups.send',
        {
          room_id: operation.room_id,
          event_id: operation.event_id,
          payload: { ...operation.payload }
        }
      )

      if (!this.current(generation)) {
        return false
      }

      if (
        result.accepted !== true ||
        result.client_event_id !== operation.event_id ||
        result.event?.room_id !== operation.room_id ||
        result.event.kind !== 'message.user' ||
        result.event.payload.text !== operation.payload.text ||
        result.event.payload.thread_id !== operation.payload.thread_id
      ) {
        throw new Error('unknownDelivery')
      }

      const key = this.key(operation.room_id)

      const cleared = await mutateReceipt(key, async () => {
        if (!this.current(generation)) {
          return false
        }

        const stored = await this.deps.storage.get<HostedSendOperation | null>(key, null)

        if (!this.current(generation)) {
          return false
        }

        if (stored !== null) {
          if (
            stored.event_id !== operation.event_id ||
            stored.scope !== operation.scope ||
            stored.room_id !== operation.room_id
          ) {
            throw new Error('storageUnavailable')
          }

          await this.deps.storage.remove(key)
        }

        if ((await this.deps.storage.get(key, null)) !== null) {
          throw new Error('storageUnavailable')
        }

        return true
      })

      if (!cleared || !this.current(generation)) {
        return false
      }

      this.patch({ pending: null, issue: null })

      return true
    } catch {
      if (this.current(generation)) {
        this.patch({ issue: 'unknownDelivery', fresh: false })
      }

      return false
    } finally {
      if (this.current(generation)) {
        this.patch({ writing: false })
        await this.refresh()
      }
    }
  }

  async stop(task: HostedTask): Promise<boolean> {
    const state = this.state.get()

    if (!this.current() || state.writing || state.loading) {
      return false
    }

    const current = state.driver?.stoppable_tasks?.find(row => row.task_id === task.task_id)

    if (
      hostedWriteIssue(state, 'groups.stop') ||
      !state.capabilities?.features.includes('exact_task_stop') ||
      !state.capabilities.methods.includes('groups.stop') ||
      !current ||
      !Number.isInteger(task.execution_generation) ||
      task.execution_generation < 0 ||
      !Number.isInteger(task.cancel_generation) ||
      task.cancel_generation < 0 ||
      current.execution_generation !== task.execution_generation ||
      current.cancel_generation !== task.cancel_generation
    ) {
      this.patch({ issue: 'staleTask' })

      return false
    }

    const generation = this.generation
    this.patch({ writing: true })

    try {
      const result = await this.request<{ stop_requested: boolean; task: HostedTask }>('groups.stop', {
        room_id: state.selected!.room_id,
        cancel_id: this.deps.uuid(),
        expected_task_id: task.task_id,
        expected_execution_generation: task.execution_generation,
        expected_cancel_generation: task.cancel_generation,
        expected_authority_gateway_id: state.selected!.authority_gateway_id,
        expected_authority_epoch: state.selected!.authority_epoch
      })

      if (
        result.stop_requested !== true ||
        result.task?.task_id !== task.task_id ||
        result.task.execution_generation !== task.execution_generation ||
        result.task.cancel_generation !== task.cancel_generation + 1 ||
        !['cancelled', 'stopping'].includes(result.task.status)
      ) {
        throw new Error('staleTask')
      }

      return true
    } catch {
      if (this.current(generation)) {
        this.patch({ issue: 'staleTask', fresh: false })
      }

      return false
    } finally {
      if (this.current(generation)) {
        this.patch({ writing: false })
        await this.refresh()
      }
    }
  }
}

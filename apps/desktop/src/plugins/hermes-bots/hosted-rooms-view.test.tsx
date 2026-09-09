import type * as HermesSdk from '@hermes/plugin-sdk'
import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import type { ComponentProps } from 'react'
import { afterEach, expect, it, vi } from 'vitest'

import { HostedRoomsController } from './hosted-rooms'
import type { HostedEvent, HostedRoom } from './hosted-rooms'
import { HostedRoomsContent } from './hosted-rooms-view'

vi.mock('@hermes/plugin-sdk', async importOriginal => {
  const sdk = await importOriginal<typeof HermesSdk>()
  const RealStreamdown = sdk.Streamdown

  function TestStreamdown(props: ComponentProps<typeof RealStreamdown>) {
    if (props.children === 'force markdown failure') {
      throw new Error('markdown failed')
    }

    return <RealStreamdown {...props} />
  }

  return { ...sdk, Streamdown: TestStreamdown, usePluginI18n: () => (key: string) => key }
})

afterEach(() => {
  cleanup()
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
})

function stubResizeObserver() {
  vi.stubGlobal(
    'ResizeObserver',
    class {
      observe() {}
      unobserve() {}
      disconnect() {}
    }
  )
}

function readyController(initialRoom: HostedRoom, initialEvents: HostedEvent[] = []) {
  let room = initialRoom
  const storage = new Map<string, unknown>()
  let id = 0

  const request = vi.fn(async (method: string, params: Record<string, unknown> = {}) => {
    if (method === 'groups.capabilities') {
      return {
        protocol_version: 2,
        authority_gateway_id: room.authority_gateway_id,
        features: ['room_identity', 'authority_epoch', 'monotonic_log', 'idempotent_send'],
        methods: ['groups.list', 'groups.state', 'groups.log', 'groups.send'],
        driver: true,
        persistent_process: true,
        max_log_limit: 500
      }
    }

    if (method === 'groups.list') {
      return { rooms: [room], next_offset: null }
    }

    if (method === 'groups.state') {
      return { room, driver_status: { working: false } }
    }

    if (method === 'groups.log') {
      return {
        events: initialEvents.filter(event => event.seq > Number(params.since_seq)),
        cursor: initialEvents.at(-1)?.seq ?? 0,
        latest_seq: initialEvents.at(-1)?.seq ?? 0,
        has_more: false,
        authority: { gateway_id: room.authority_gateway_id, epoch: room.authority_epoch }
      }
    }

    if (method === 'groups.send') {
      const payload = params.payload as Record<string, unknown>

      return {
        accepted: true,
        client_event_id: params.event_id,
        event: {
          room_id: room.room_id,
          seq: initialEvents.length + 1,
          event_id: params.event_id,
          kind: 'message.user',
          actor: { kind: 'user', id: 'desktop' },
          authority_epoch: room.authority_epoch,
          payload,
          created_at: 1000
        }
      }
    }

    throw new Error(`Unexpected method ${method}`)
  })

  const controller = new HostedRoomsController({
    scope: 'connection-a',
    isCurrent: () => true,
    uuid: () => `op-${++id}`,
    request: async <T,>(method: string, params?: Record<string, unknown>) => (await request(method, params)) as T,
    storage: {
      get: <T,>(key: string, fallback: T) => (storage.has(key) ? (storage.get(key) as T) : fallback),
      set: (key, value) => {
        storage.set(key, value)
      },
      remove: key => {
        storage.delete(key)
      }
    }
  })

  return {
    controller,
    request,
    setRoom(next: HostedRoom) {
      room = next
    }
  }
}

it('renders only hosted members and freezes the original message while an uncertain send awaits explicit retry', async () => {
  stubResizeObserver()

  const room: HostedRoom = {
    room_id: 'hosted-a',
    name: 'Room A',
    authority_gateway_id: 'install:a',
    authority_epoch: 1,
    members: [
      { member_id: 'a', profile: 'a', handle: 'a', display_name: 'Iris' },
      { member_id: 'b', profile: 'b', handle: 'b', display_name: 'Vox' }
    ]
  }

  const events: HostedEvent[] = []
  const storage = new Map<string, unknown>()
  let id = 0

  const request = vi.fn(async (method: string, params: Record<string, unknown> = {}) => {
    if (method === 'groups.capabilities') {
      return {
        protocol_version: 2,
        authority_gateway_id: 'install:a',
        features: ['room_identity', 'authority_epoch', 'monotonic_log', 'idempotent_send'],
        methods: ['groups.list', 'groups.state', 'groups.log', 'groups.send'],
        driver: true,
        persistent_process: true,
        max_log_limit: 500
      }
    }

    if (method === 'groups.list') {
      return { rooms: [room], next_offset: null }
    }

    if (method === 'groups.state') {
      return { room, driver_status: { working: true } }
    }

    if (method === 'groups.log') {
      return {
        events: events.filter(event => event.seq > Number(params.since_seq)),
        cursor: events.length,
        latest_seq: events.length,
        has_more: false,
        authority: { gateway_id: 'install:a', epoch: 1 }
      }
    }

    if (method === 'groups.send') {
      if (!events.length) {
        events.push({
          room_id: room.room_id,
          seq: 1,
          event_id: 'server-a',
          kind: 'message.user',
          actor: { kind: 'user', id: 'desktop' },
          authority_epoch: 1,
          payload: params.payload as Record<string, unknown>,
          created_at: 1000
        })
      }

      throw new Error('Reply lost')
    }

    throw new Error(`Unexpected method ${method}`)
  })

  const controller = new HostedRoomsController({
    scope: 'connection-a',
    isCurrent: () => true,
    uuid: () => `op-${++id}`,
    request: async <T,>(method: string, params?: Record<string, unknown>) => (await request(method, params)) as T,
    storage: {
      get: <T,>(key: string, fallback: T) => (storage.has(key) ? (storage.get(key) as T) : fallback),
      set: (key, value) => {
        storage.set(key, value)
      },
      remove: key => {
        storage.delete(key)
      }
    }
  })

  await controller.list()
  render(<HostedRoomsContent controller={controller} online sourceLabel="Team gateway" />)
  fireEvent.click(screen.getByRole('button', { name: /Room A/ }))
  await screen.findByRole('textbox', { name: 'hostedRooms.message' })
  await waitFor(() => expect(controller.state.get().fresh).toBe(true))
  expect(screen.getAllByRole('checkbox')).toHaveLength(3)
  fireEvent.click(screen.getByRole('checkbox', { name: 'Vox' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'hostedRooms.message' }), { target: { value: 'Please review' } })
  fireEvent.click(screen.getByRole('button', { name: 'hostedRooms.send' }))
  await screen.findByText('hostedRooms.pending')
  expect(screen.queryByRole('textbox', { name: 'hostedRooms.message' })).toBeNull()
  expect(request.mock.calls.filter(([method]) => method === 'groups.send')).toHaveLength(1)
  await waitFor(() => expect(controller.state.get().loading).toBe(false))
  fireEvent.click(screen.getByRole('button', { name: 'hostedRooms.retry' }))
  await waitFor(() => expect(request.mock.calls.filter(([method]) => method === 'groups.send')).toHaveLength(2))
  const sends = request.mock.calls.filter(([method]) => method === 'groups.send')
  expect(sends[0][1]).toEqual(sends[1][1])
  expect(sends[0][1]?.payload).toEqual({ text: '@a Please review', thread_id: expect.any(String) })
  await act(async () => {
    await Promise.resolve()
  })
  expect(events).toHaveLength(1)
})

it('resets the composer and recipient selection when the room authority changes', async () => {
  stubResizeObserver()

  const first: HostedRoom = {
    room_id: 'hosted-a',
    name: 'Room A',
    authority_gateway_id: 'install:a',
    authority_epoch: 1,
    members: [
      { member_id: 'a', profile: 'a', handle: 'a', display_name: 'Iris' },
      { member_id: 'b', profile: 'b', handle: 'b', display_name: 'Vox' }
    ]
  }

  const second: HostedRoom = {
    ...first,
    authority_epoch: 2,
    members: [
      { member_id: 'c', profile: 'c', handle: 'c', display_name: 'Nova' },
      { member_id: 'd', profile: 'd', handle: 'd', display_name: 'Rune' }
    ]
  }

  const fixture = readyController(first)

  await fixture.controller.list()
  render(<HostedRoomsContent controller={fixture.controller} online sourceLabel="Team gateway" />)
  fireEvent.click(screen.getByRole('button', { name: /Room A/ }))
  const composer = await screen.findByRole('textbox', { name: 'hostedRooms.message' })
  await waitFor(() => expect(fixture.controller.state.get().fresh).toBe(true))
  fireEvent.click(screen.getByRole('checkbox', { name: 'Vox' }))
  fireEvent.change(composer, { target: { value: 'old authority draft' } })

  fixture.setRoom(second)
  act(() => {
    fixture.controller.state.set({
      ...fixture.controller.state.get(),
      selected: second,
      authority: { gateway_id: second.authority_gateway_id, epoch: second.authority_epoch },
      issue: null,
      fresh: true
    })
  })

  expect((screen.getByRole('textbox', { name: 'hostedRooms.message' }) as HTMLTextAreaElement).value).toBe('')
  expect(screen.getByRole('checkbox', { name: 'Nova' }).getAttribute('data-state')).toBe('checked')
  expect(screen.getByRole('checkbox', { name: 'Rune' }).getAttribute('data-state')).toBe('checked')
  expect(screen.getByRole('checkbox', { name: 'hostedRooms.everyone' }).getAttribute('data-state')).toBe('checked')

  fireEvent.click(screen.getByRole('checkbox', { name: 'Rune' }))
  fireEvent.change(screen.getByRole('textbox', { name: 'hostedRooms.message' }), { target: { value: 'new draft' } })
  fireEvent.click(screen.getByRole('button', { name: 'hostedRooms.send' }))
  await waitFor(() => expect(fixture.request.mock.calls.some(([method]) => method === 'groups.send')).toBe(true))
  const send = fixture.request.mock.calls.find(([method]) => method === 'groups.send')
  expect(send?.[1]?.payload).toEqual({ text: '@c new draft', thread_id: expect.any(String) })
})

it('drops removed recipient ids and computes Everyone from the current member set', async () => {
  stubResizeObserver()

  const first: HostedRoom = {
    room_id: 'hosted-a',
    name: 'Room A',
    authority_gateway_id: 'install:a',
    authority_epoch: 1,
    members: [
      { member_id: 'a', profile: 'a', handle: 'a', display_name: 'Iris' },
      { member_id: 'b', profile: 'b', handle: 'b', display_name: 'Vox' }
    ]
  }

  const replacement: HostedRoom = {
    ...first,
    members: [
      { member_id: 'c', profile: 'c', handle: 'c', display_name: 'Nova' },
      { member_id: 'd', profile: 'd', handle: 'd', display_name: 'Rune' }
    ]
  }

  const fixture = readyController(first)

  await fixture.controller.list()
  render(<HostedRoomsContent controller={fixture.controller} online sourceLabel="Team gateway" />)
  fireEvent.click(screen.getByRole('button', { name: /Room A/ }))
  await screen.findByRole('textbox', { name: 'hostedRooms.message' })
  await waitFor(() => expect(fixture.controller.state.get().fresh).toBe(true))

  act(() => {
    fixture.controller.state.set({ ...fixture.controller.state.get(), selected: replacement })
  })
  await waitFor(() =>
    expect(screen.getByRole('checkbox', { name: 'hostedRooms.everyone' }).getAttribute('data-state')).toBe('unchecked')
  )
  expect(screen.getByRole('checkbox', { name: 'Nova' }).getAttribute('data-state')).toBe('unchecked')
  expect(screen.getByRole('checkbox', { name: 'Rune' }).getAttribute('data-state')).toBe('unchecked')
  fireEvent.change(screen.getByRole('textbox', { name: 'hostedRooms.message' }), { target: { value: 'hello' } })
  expect((screen.getByRole('button', { name: 'hostedRooms.send' }) as HTMLButtonElement).disabled).toBe(true)
})

it('renders a persisted 5,000-level HTML message without disabling the room controls', async () => {
  stubResizeObserver()

  const room: HostedRoom = {
    room_id: 'hosted-a',
    name: 'Room A',
    authority_gateway_id: 'install:a',
    authority_epoch: 1,
    members: [{ member_id: 'a', profile: 'a', handle: 'a', display_name: 'Iris' }]
  }

  const pathologicalText = `@all ${'<div>'.repeat(5000)}room message`

  const event: HostedEvent = {
    room_id: room.room_id,
    seq: 1,
    event_id: 'persisted-event',
    kind: 'message.member',
    actor: { kind: 'member', id: 'a' },
    authority_epoch: 1,
    payload: { text: pathologicalText },
    created_at: 1000
  }

  const fixture = readyController(room, [event])

  await fixture.controller.list()
  render(<HostedRoomsContent controller={fixture.controller} online sourceLabel="Team gateway" />)
  fireEvent.click(screen.getByRole('button', { name: /Room A/ }))
  await screen.findByRole('textbox', { name: 'hostedRooms.message' })
  await waitFor(() => expect(fixture.controller.state.get().fresh).toBe(true))

  expect(new TextEncoder().encode(pathologicalText)).toHaveLength(25_017)
  expect(screen.getByRole('log').textContent).toContain('room message')
  const composer = screen.getByRole('textbox', { name: 'hostedRooms.message' })
  fireEvent.change(composer, { target: { value: 'still usable' } })
  expect((composer as HTMLTextAreaElement).value).toBe('still usable')
  expect((screen.getByRole('button', { name: 'hostedRooms.reload' }) as HTMLButtonElement).disabled).toBe(false)
})

it('keeps the original event text and room controls when markdown rendering throws', async () => {
  stubResizeObserver()
  vi.spyOn(console, 'error').mockImplementation(() => undefined)

  const room: HostedRoom = {
    room_id: 'hosted-a',
    name: 'Room A',
    authority_gateway_id: 'install:a',
    authority_epoch: 1,
    members: [{ member_id: 'a', profile: 'a', handle: 'a', display_name: 'Iris' }]
  }

  const event: HostedEvent = {
    room_id: room.room_id,
    seq: 1,
    event_id: 'persisted-event',
    kind: 'message.member',
    actor: { kind: 'member', id: 'a' },
    authority_epoch: 1,
    payload: { text: 'force markdown failure' },
    created_at: 1000
  }

  const fixture = readyController(room, [event])

  await fixture.controller.list()
  render(<HostedRoomsContent controller={fixture.controller} online sourceLabel="Team gateway" />)
  fireEvent.click(screen.getByRole('button', { name: /Room A/ }))
  await screen.findByRole('textbox', { name: 'hostedRooms.message' })
  await waitFor(() => expect(fixture.controller.state.get().fresh).toBe(true))

  expect(screen.getByText('force markdown failure')).not.toBeNull()
  expect(screen.getByRole('textbox', { name: 'hostedRooms.message' })).not.toBeNull()
  expect(screen.getByRole('button', { name: 'hostedRooms.reload' })).not.toBeNull()
})

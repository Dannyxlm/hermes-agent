import { useStore } from '@nanostores/react'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, beforeEach, expect, test, vi } from 'vitest'

import { discardLostPrompt } from '@/app/chat/composer/discard-lost-prompt'
import { renderComposerQueueSlot } from '@/app/chat/composer/queue-slot'
import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import { $queuedPromptsBySession, getQueuedPrompts } from '@/store/composer-queue'
import { $activeGatewayProfile } from '@/store/profile'
import { $connection, $sessions } from '@/store/session'

import { canonicalUnknownRecovery } from './canonical-unknown-recovery'
import { HermesGateway } from './client'

const originalSocket = globalThis.WebSocket
beforeEach(() => {
  window.localStorage.clear()
  $queuedPromptsBySession.set({})
  $sessions.set([])
  $connection.set(null)
  $activeGatewayProfile.set('default')
})
afterEach(() => { cleanup(); globalThis.WebSocket = originalSocket; $activeGatewayProfile.set('default'); vi.restoreAllMocks() })

function RecoveryQueue({ client }: { client: HermesGateway }) {
  const queues = useStore($queuedPromptsBySession)

  return renderComposerQueueSlot({ activeQueueSessionKey: 'chat', beginQueuedEdit: vi.fn(), busy: false,
    drainNextQueued: vi.fn(), exitQueuedEdit: vi.fn(), gateway: client, queueEdit: null, queueParked: false,
    queuedPrompts: queues.chat ?? [], sendQueuedNow: vi.fn(), sessionId: 'chat', steerQueuedNow: vi.fn(), t: en })
}

async function fixture(patch: Record<string, unknown> = {}, refusal?: string, profile = 'default') {
  const wsPackage = 'ws'
  const { WebSocket, WebSocketServer } = await import(wsPackage)
  globalThis.WebSocket = WebSocket
  const server = new WebSocketServer({ host: '127.0.0.1', port: 0 })
  await new Promise<void>(resolve => server.once('listening', resolve))
  const frames: Array<{ method: string; params: Record<string, unknown> }> = []

  const receipt = { admission_id: 'lost', ref: { profile_id: '/opaque/tenants/home-73', session_id: 'chat' }, sequence: 1,
    status: 'unknown', outcome: null, authority_epoch: 1, execution_generation: 4, controls: ['resolve_unknown'], ...patch }

  let discarded = false
  let terminalElsewhere = false
  let receiptRefusal = refusal
  server.on('connection', (socket: any) => {
    socket.on('message', (bytes: Buffer) => {
      const frame = JSON.parse(bytes.toString())
      frames.push({ method: frame.method, params: frame.params })

      if (frame.method === 'prompt.receipt' && receiptRefusal) {
        socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, error: { code: 4090, message: receiptRefusal, data: { reason: receiptRefusal } } }))

        return
      }

      const result = frame.method === 'session.resume'
        ? { session_id: 'chat', stored_session_id: 'chat', execution_generation: 9, revision: 1, replay_epoch: 'epoch', last_sequence: 3,
            info: { writable: true, profile_id: '/opaque/tenants/home-73', profile_name: profile }, messages: [], pending: [
              { ...receipt, admission_id: 'lost', ref: { profile_id: '/opaque/tenants/home-73', session_id: 'chat' }, status: 'unknown', execution_generation: 4, controls: [], input_id: 'prior-input', text: 'prior input' },
              { ...receipt, admission_id: 'later', sequence: 2, execution_generation: 5, controls: [], input_id: 'later-input', text: 'later input' }
            ].filter(row => !discarded || row.admission_id !== 'lost') }
        : frame.method === 'session.info' ? { stored_session_id: 'chat', writable: true }
        : frame.method === 'prompt.resolve_unknown' ? { ...receipt, status: 'terminal', outcome: 'interrupted' }
        : frame.method === 'prompt.receipt' ? (frame.params.admission_id === 'later'
            ? { ...receipt, admission_id: 'later', sequence: 2, execution_generation: 5 }
            : terminalElsewhere ? { ...receipt, status: 'terminal', controls: [], outcome: 'interrupted' } : receipt) : {}

      if (frame.method === 'prompt.resolve_unknown') { discarded = true }
      socket.send(JSON.stringify({ jsonrpc: '2.0', id: frame.id, result }))
    })
  })
  const { port } = server.address() as { port: number }
  const client = new HermesGateway()
  await client.connect(`ws://127.0.0.1:${port}/api/ws?native_dial=fixture&ticket=fixture`)

  return { client, frames, discardElsewhere() { terminalElsewhere = true }, setReceiptRefusal(value?: string) { receiptRefusal = value }, async close() {
    client.close()

    for (const socket of server.clients) { socket.terminate() }
    await new Promise<void>(resolve => server.close(() => resolve()))
  } }
}

test.each(['session.resume', 'session.activate'])('%s automatically reads only the earliest unknown metadata receipt and enables exact Discard', async method => {
  const f = await fixture()

  try {
    const resumed = await f.client.request<any>(method, { session_id: 'chat', profile: 'default', omit_messages: true })
    applyRuntimeInfo(resumed.info)
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt')).toEqual([
      { method: 'prompt.receipt', params: { session_id: 'chat', admission_id: 'lost', profile: 'default' } }
    ])
    expect(getQueuedPrompts('chat').find(entry => entry.id === 'lost')).toMatchObject({ serverControls: ['resolve_unknown'], serverExecutionGeneration: 4 })
    expect(f.frames.some(frame => frame.method === 'prompt.resolve_unknown' || frame.method === 'prompt.submit')).toBe(false)
    render(createElement(I18nProvider, { configClient: { getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) },
      children: createElement(RecoveryQueue, { client: f.client }) }))
    expect(screen.getAllByRole('button', { name: en.composer.queueLostDiscard })).toHaveLength(1)
    expect(screen.getByText('prior input')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
    await vi.waitFor(() => expect(getQueuedPrompts('chat').find(entry => entry.id === 'later')).toMatchObject({ serverControls: ['resolve_unknown'] }))
    expect(f.frames.filter(frame => frame.method === 'prompt.resolve_unknown')).toEqual([
      { method: 'prompt.resolve_unknown', params: { session_id: 'chat', admission_id: 'lost', execution_generation: 4 } }
    ])
    expect(getQueuedPrompts('chat').some(entry => entry.id === 'lost')).toBe(false)
    expect(getQueuedPrompts('chat').find(entry => entry.id === 'later')).toMatchObject({ serverControls: ['resolve_unknown'], serverExecutionGeneration: 5 })
    expect(f.frames.filter(frame => frame.method === 'prompt.resolve_unknown')).toHaveLength(1)
  } finally { await f.close() }
})

test.each([
  { ref: { profile_id: '/opaque/homes/foreign', session_id: 'chat' } },
  { ref: { profile_id: '/opaque/homes/default', session_id: 'wrong-target' } },
  { admission_id: 'wrong-admission' }, { execution_generation: 3 }, { execution_generation: 5 },
  { controls: [] }, { controls: undefined }, { controls: ['interrupt'] }, { status: 'started' }
])('canonical metadata recovery cannot grant Discard from mismatched/unserved evidence %j', async patch => {
  const f = await fixture(patch)

  try {
    const resumed = await f.client.request<any>('session.resume', { session_id: 'chat', profile: 'default' })
    applyRuntimeInfo(resumed.info)
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt')).toHaveLength(1)
    expect(getQueuedPrompts('chat').every(entry => !entry.serverControls?.includes('resolve_unknown'))).toBe(true)
    expect(f.frames.some(frame => frame.method === 'prompt.submit' || frame.method === 'prompt.resolve_unknown')).toBe(false)
  } finally { await f.close() }
})

test.each(['permission_denied', 'wrong_user', 'not_found', 'method_not_found', 'owner_unavailable'])('unavailable canonical metadata (%s) preserves a successful nonretryable mount without granting Discard', async refusal => {
  const f = await fixture({}, refusal)

  try {
    const resumed = await f.client.request<any>('session.resume', { session_id: 'chat', profile: 'default' })
    applyRuntimeInfo(resumed.info)
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt')).toHaveLength(1)
    expect(getQueuedPrompts('chat').every(entry => !entry.serverControls?.includes('resolve_unknown'))).toBe(true)
    expect(f.frames.some(frame => frame.method === 'prompt.submit' || frame.method === 'prompt.resolve_unknown')).toBe(false)
  } finally { await f.close() }
})

test('terminal metadata from another viewer advances info to the next barrier without Discard or replay', async () => {
  const f = await fixture()

  try {
    applyRuntimeInfo((await f.client.request<any>('session.resume', { session_id: 'chat' })).info)
    f.discardElsewhere()
    f.frames.length = 0
    applyRuntimeInfo(await f.client.request<any>('session.info', { session_id: 'chat' }))
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt').map(frame => frame.params.admission_id)).toEqual(['lost', 'later'])
    expect(getQueuedPrompts('chat')).toMatchObject([{ id: 'later', serverControls: ['resolve_unknown'] }])
    f.frames.length = 0
    await f.client.request('session.info', { session_id: 'chat' })
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt').map(frame => frame.params.admission_id)).toEqual(['later'])
    expect(f.frames.some(frame => frame.method === 'prompt.resolve_unknown' || frame.method === 'prompt.submit')).toBe(false)
  } finally { await f.close() }
})

test.each([undefined, 'default'])('a named-launch route (%s) uses the learned owner for receipt and Discard', async route => {
  const f = await fixture({}, undefined, 'background')
  $activeGatewayProfile.set('background')

  try {
    const resumed = await f.client.request<any>('session.resume', { session_id: 'chat', ...(route ? { profile: route } : {}) })
    applyRuntimeInfo(resumed.info)
    expect(getQueuedPrompts('chat').find(row => row.id === 'lost')).toMatchObject({ serverControls: ['resolve_unknown'] })
    await discardLostPrompt('chat', 'chat', 'lost', f.client.request.bind(f.client), 4)
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt')[0].params.profile).toBe('background')
    expect(f.frames.filter(frame => frame.method === 'prompt.resolve_unknown')[0].params.profile).toBe('background')
  } finally { await f.close() }
})

test('canonical session.info rechecks the retained earliest unknown through the scoped metadata receipt seam', async () => {
  const f = await fixture()

  try {
    await f.client.request('session.resume', { session_id: 'chat', profile: 'default' })
    f.frames.length = 0
    const info = await f.client.request<any>('session.info', { session_id: 'chat', profile: 'default' })
    applyRuntimeInfo(info)
    expect(f.frames.map(frame => frame.method)).toEqual(['session.info', 'prompt.receipt'])
    expect(getQueuedPrompts('chat').find(entry => entry.id === 'lost')).toMatchObject({ serverControls: ['resolve_unknown'], serverExecutionGeneration: 4 })
  } finally { await f.close() }
})


test('a named-profile canonical owner recovers and discards only on its own route', async () => {
  const f = await fixture({}, undefined, 'background')
  $activeGatewayProfile.set('background')

  try {
    const resumed = await f.client.request<any>('session.resume', { session_id: 'chat', profile: 'background' })
    applyRuntimeInfo(resumed.info)
    await discardLostPrompt('chat', 'chat', 'lost', f.client.request.bind(f.client), 4)
    expect(f.frames.filter(frame => frame.method === 'prompt.receipt')[0]).toEqual({ method: 'prompt.receipt',
      params: { session_id: 'chat', admission_id: 'lost', profile: 'background' } })
    expect(f.frames.filter(frame => frame.method === 'prompt.resolve_unknown')).toEqual([{ method: 'prompt.resolve_unknown',
      params: { session_id: 'chat', admission_id: 'lost', execution_generation: 4, profile: 'background' } }])
  } finally { await f.close() }
})

test('metadata recovery never reads user/input_id/result content, even when supplied by a receipt', async () => {
  const identity = { admission_id: 'lost', session_id: 'chat', execution_generation: 4 }

  const receipt = { admission_id: identity.admission_id, execution_generation: identity.execution_generation,
    ref: { profile_id: '/opaque/owner-home', session_id: 'chat' }, status: 'unknown', controls: ['resolve_unknown'] }

  for (const name of ['user', 'input_id', 'result']) {
    Object.defineProperty(receipt, name, { get: () => { throw new Error('private content read') } })
  }

  const request = vi.fn(async () => receipt)
  await expect(canonicalUnknownRecovery(identity, request, { profile: 'default' }, raw => raw === '/opaque/owner-home')).resolves.toEqual(receipt)
  expect(request).toHaveBeenCalledWith('prompt.receipt', { session_id: 'chat', profile: 'default', admission_id: 'lost' })
})


test('an unavailable metadata recheck revokes permission but does not manufacture terminal retirement', async () => {
  const f = await fixture()

  try {
    const resumed = await f.client.request<any>('session.resume', { session_id: 'chat', profile: 'default' })
    applyRuntimeInfo(resumed.info)
    f.setReceiptRefusal('owner_unavailable')
    applyRuntimeInfo(await f.client.request<any>('session.info', { session_id: 'chat', profile: 'default' }))
    expect(getQueuedPrompts('chat').every(entry => !entry.serverControls?.includes('resolve_unknown'))).toBe(true)
    f.setReceiptRefusal()
    applyRuntimeInfo(await f.client.request<any>('session.info', { session_id: 'chat', profile: 'default' }))
    expect(getQueuedPrompts('chat').find(entry => entry.id === 'lost')).toMatchObject({ serverControls: ['resolve_unknown'], serverExecutionGeneration: 4 })
    expect(f.frames.some(frame => frame.method === 'prompt.submit' || frame.method === 'prompt.resolve_unknown')).toBe(false)
  } finally { await f.close() }
})

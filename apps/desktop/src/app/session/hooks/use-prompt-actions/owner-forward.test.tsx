import { JsonRpcGatewayError } from '@hermes/shared'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { renderComposerQueueSlot } from '@/app/chat/composer/queue-slot'
import type { HermesGateway } from '@/hermes'
import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import { errorCardActions, errorCardOffers, type ErrorSurface } from '@/lib/error-surface'
import { $composerAttachments } from '@/store/composer'
import { $queuedPromptsBySession, getQueuedPrompts } from '@/store/composer-queue'
import { clearNotifications } from '@/store/notifications'
import { $readOnlyStoredTranscripts, $sessionWriteVerdicts, isStoredTranscriptReadOnly } from '@/store/read-only-transcript'
import { $busy, $connection, $currentCwd } from '@/store/session'

import { actRender, Harness, type HarnessHandle } from './index-test-utils'

// RT-37 (#140385): a prompt on a chat whose gateway owner is mid-turn.
describe('usePromptActions gateway-owner queue (#140385)', () => {
  afterEach(() => {
    cleanup()
    clearNotifications()
    $composerAttachments.set([])
    $readOnlyStoredTranscripts.set(new Set()); $sessionWriteVerdicts.set(new Map())
    $queuedPromptsBySession.set({})
    localStorage.clear()
  })

  it('re-uploads the image of a draft restored after a SESSION_OWNER_BUSY refusal: serve dropped it (#140968)', async () => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/root')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileDataUrl: vi.fn(async () => 'data:image/jpeg;base64,aGVsbG8=') }
    })
    $composerAttachments.set([
      {
        detail: '/home/alice/photo.jpg',
        id: 'image:photo.jpg',
        kind: 'image',
        label: 'photo.jpg',
        occurrenceId: 'occurrence-photo',
        path: '/home/alice/photo.jpg'
      }
    ])

    const calls: string[] = []
    let submits = 0

    const requestGateway = vi.fn(async (method: string) => {
      calls.push(method)

      if (method === 'image.attach_bytes' || method === 'image.attach') {
        return { attached: true, path: '/root/tmp/photo.jpg' } as never
      }

      if (method === 'prompt.submit' && ++submits === 1) {
        throw new JsonRpcGatewayError(
          'The gateway is running a turn in this chat and an attachment cannot join its queue. The image was removed. Nothing was sent.',
          { code: 4090, data: { reason: 'SESSION_OWNER_BUSY' } }
        )
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness onReady={h => (handle = h)} refreshSessions={async () => undefined} requestGateway={requestGateway} />
    )

    expect(await handle!.submitText('describe this')).toBe(false)
    expect($composerAttachments.get()).toHaveLength(1)

    await handle!.submitText('describe this')

    const uploads = (m: string) => m === 'image.attach_bytes' || m === 'image.attach'
    const firstRefusal = calls.indexOf('prompt.submit')
    const resend = calls.lastIndexOf('prompt.submit')
    expect(resend).toBeGreaterThan(firstRefusal)
    expect(calls.slice(firstRefusal + 1, resend).some(uploads)).toBe(true)
  })

  it('turns a SESSION_OWNER_BUSY refusal into a retryable card, not "Start new session" (#140385)', async () => {
    let latest: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') {
        throw new JsonRpcGatewayError(
          'The gateway is running a turn in this chat and a rewind or attachment cannot join its queue. Nothing was sent. Send it again when the current turn ends.',
          { code: 4090, data: { reason: 'SESSION_OWNER_BUSY' } }
        )
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        onSeedState={next => {
          latest = next
        }}
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )

    expect(await handle!.submitText('continue here')).toBe(false)
    const bubble = (latest?.messages as { errorSurface?: Record<string, unknown> }[]).at(-1)
    expect(bubble?.errorSurface).toEqual({ layer: 'gateway', code: 'SESSION_OWNER_BUSY', retryable: true })
  })

  it('turns an unconfirmed forward into a card without Retry: the owner may already hold it', async () => {
    let latest: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') {
        throw new JsonRpcGatewayError(
          'The gateway is running a turn in this chat and did not confirm your message in time. It may already be queued there: check the chat before sending it again.',
          { code: 4090, data: { reason: 'SESSION_OWNER_UNCONFIRMED' } }
        )
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        onSeedState={next => {
          latest = next
        }}
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )

    expect(await handle!.submitText('continue here')).toBe(false)
    const bubble = (latest?.messages as { errorSurface?: ErrorSurface }[]).at(-1)
    expect(bubble?.errorSurface).toEqual({ layer: 'gateway', code: 'SESSION_OWNER_UNCONFIRMED', retryable: false })
    expect(errorCardOffers(bubble?.errorSurface, 'retry')).toBe(false)
  })

  it.each([
    ['owner_live_elsewhere', 'SESSION_NOT_OWNED', 'start_new_session'],
    ['owner_unavailable', 'SESSION_OWNER_UNAVAILABLE', 'resume_session'],
    ['outcome_unknown', 'SESSION_OUTCOME_UNKNOWN', 'resume_session']
  ])('uses the %s recovery without resending', async (reason, code, action) => {
    let latest: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') {
        throw new JsonRpcGatewayError('ownership refusal', { code: 4090, data: { reason: 'SESSION_NOT_OWNED', ownership_reason: reason } })
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} onSeedState={next => { latest = next }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    expect(await handle!.submitText('my question')).toBe(false)
    const bubble = (latest?.messages as { errorSurface?: ErrorSurface }[]).at(-1)
    expect(bubble?.errorSurface).toEqual({ layer: 'gateway', code, retryable: false })
    expect(errorCardOffers(bubble?.errorSurface, 'retry')).toBe(false)
    expect(errorCardActions(bubble?.errorSurface).primary).toBe(action)
    expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(1)
  })

  it('projects the other viewer barrier directly from the typed unknown refusal, never as this send', async () => {
    let latest: Record<string, unknown> | undefined

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') { throw new JsonRpcGatewayError('Unknown owner turn', { code: 4090, data: {
        reason: 'SESSION_NOT_OWNED', ownership_reason: 'outcome_unknown', blocking_unknown: [{
          admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'rt-abc123' },
          status: 'unknown', execution_generation: 11, controls: ['resolve_unknown']
        }]
      } }) }

      if (method === 'prompt.resolve_unknown') { return { admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'rt-abc123' },
        status: 'terminal', outcome: 'discarded', execution_generation: 11, authority_epoch: 2, sequence: 1 } as never }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} onSeedState={next => { latest = next }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    expect(await handle!.submitText('refused question must never replay')).toBe(false)
    expect(getQueuedPrompts('rt-abc123')).toMatchObject([{
      id: 'other-viewer', text: '', serverStatus: 'unknown', serverExecutionGeneration: 11, serverControls: ['resolve_unknown']
    }])
    const error = (latest?.messages as { errorSurface?: ErrorSurface }[]).at(-1)
    expect(errorCardOffers(error?.errorSurface, 'retry')).toBe(false)
    expect(errorCardActions(error?.errorSurface).primary).not.toBe('new-session')
    const sendQueuedNow = vi.fn()
    render(<I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
      {renderComposerQueueSlot({ activeQueueSessionKey: 'rt-abc123', beginQueuedEdit: vi.fn(), busy: false,
        drainNextQueued: vi.fn(), exitQueuedEdit: vi.fn(), gateway: { request: requestGateway } as unknown as HermesGateway,
        queueEdit: null, queueParked: false, queuedPrompts: getQueuedPrompts('rt-abc123'), sendQueuedNow,
        sessionId: 'rt-abc123', steerQueuedNow: vi.fn(), t: en })}
    </I18nProvider>)
    fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
    await vi.waitFor(() => expect(getQueuedPrompts('rt-abc123')).toEqual([]))
    expect(requestGateway).toHaveBeenCalledWith('prompt.resolve_unknown', { session_id: 'rt-abc123', admission_id: 'other-viewer', execution_generation: 11 })
    expect(sendQueuedNow).not.toHaveBeenCalled()
    await handle!.cancelRun()
    expect(requestGateway).toHaveBeenLastCalledWith('session.interrupt', { session_id: 'rt-abc123' })
    expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(1)
  })

  it('projects an observed barrier from the real session.info event consumer with no pending input', async () => {
    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={async () => ({} as never)} />)
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: {
      stored_session_id: 'rt-abc123', pending_submissions: [], blocking_unknown: [{ admission_id: 'observed',
        ref: { profile_id: 'default', session_id: 'rt-abc123' }, status: 'unknown', execution_generation: 9, controls: ['resolve_unknown'] }]
    } })
    expect(getQueuedPrompts('rt-abc123')).toMatchObject([{ id: 'observed', text: '', serverExecutionGeneration: 9 }])
  })

  it('a late A refusal is journaled under captured A, never painted into selected B', async () => {
    let rejectSubmit!: (error: unknown) => void
    let entered!: () => void
    const started = new Promise<void>(resolve => { entered = resolve })
    const active = { current: 'rt-abc123' as string | null }
    const selected = { current: 'rt-abc123' as string | null }

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') { entered();

 return await new Promise<never>((_resolve, reject) => { rejectSubmit = reject }) }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(<Harness activeSessionIdRef={active} onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} selectedStoredSessionIdRef={selected} />)
    const send = handle!.submitTextRaw('A question')
    await started
    active.current = 'B-runtime'; selected.current = 'B-stored'
    rejectSubmit(new JsonRpcGatewayError('Unknown owner turn', { code: 4090, data: { reason: 'SESSION_NOT_OWNED', ownership_reason: 'outcome_unknown',
      blocking_unknown: [{ admission_id: 'A-barrier', ref: { profile_id: 'default', session_id: 'rt-abc123' }, status: 'unknown', execution_generation: 11, controls: ['resolve_unknown'] }] } }))
    expect(await send).toBe(false)
    expect(getQueuedPrompts('B-stored')).toEqual([])
    expect(getQueuedPrompts('rt-abc123')).toMatchObject([{ id: 'A-barrier', serverExecutionGeneration: 11 }])
    expect(active.current).toBe('B-runtime')
  })

  it('disables a successful history mount before send and refreshes the authority verdict for owned/CLI chats', async () => {
    const requestGateway = vi.fn(async () => ({} as never))
    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: { stored_session_id: 'rt-abc123', writable: false, read_only_reason: 'session_read_only' } })
    expect(isStoredTranscriptReadOnly('rt-abc123')).toBe(true)
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', connectionId: 'foreign-gateway', profile: 'default', payload: { stored_session_id: 'rt-abc123', writable: true } })
    expect(isStoredTranscriptReadOnly('rt-abc123')).toBe(true)
    expect(await handle!.submitText('must stay unsent')).toBe(false)
    expect(requestGateway).not.toHaveBeenCalled()
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: { stored_session_id: 'rt-abc123', writable: true, read_only_reason: null } })
    expect(isStoredTranscriptReadOnly('rt-abc123')).toBe(false)
    expect(await handle!.submitText('owned continuation')).toBe(true)
  })

  it.each([
    { generation: 7, controls: ['interrupt'], forwarded: true },
    { generation: 8, controls: ['interrupt'], forwarded: false },
    { generation: 7, controls: [], forwarded: false }
  ])('the main Stop attaches only the current served forwarded generation: %j', async ({ generation, controls, forwarded }) => {
    const requestGateway = vi.fn(async () => ({} as never))
    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    await handle!.submitText('forwarded start')
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: { stored_session_id: 'rt-abc123', pending_submissions: [
      { admission_id: 'active-forward', input_id: 'input', status: 'started', user: 'running', execution_generation: 7, controls }
    ] } })
    handle!.handleEvent({ type: 'message.start', session_id: 'rt-abc123', admission_id: 'active-forward', authority_epoch: 1, execution_generation: generation, payload: {} })
    await handle!.cancelRun()
    expect(requestGateway).toHaveBeenLastCalledWith('session.interrupt', { session_id: 'rt-abc123', ...(forwarded ? { admission_id: 'active-forward', execution_generation: 7 } : {}) })
  })

  it.each([false, true])('stale forwarded A cannot divert Stop from the current local C (stamped=%s)', async stamped => {
    let localRunning = false

    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'prompt.submit') { localRunning = true }

      // The real server dispatches an explicit admission to A, not its local running slot.
      if (method === 'session.interrupt' && !params?.admission_id) { localRunning = false }

      return {} as never
    })

    let handle: HarnessHandle | null = null

    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: { stored_session_id: 'rt-abc123', pending_submissions: [
      { admission_id: 'stale-A', status: 'started', user: 'A', execution_generation: 7, controls: ['interrupt'] }
    ] } })
    await handle!.submitText('local C')
    handle!.handleEvent({ type: 'message.start', session_id: 'rt-abc123', ...(stamped ? { authority_epoch: 2, execution_generation: 7, admission_id: 'local-C' } : {}), payload: {} })
    expect(localRunning).toBe(true)
    expect(handle!.state().busy).toBe(true)
    await handle!.cancelRun()
    expect(requestGateway).toHaveBeenLastCalledWith('session.interrupt', { session_id: 'rt-abc123' })
    expect(localRunning).toBe(false)
    expect(handle!.state().busy).toBe(false)
  })

  it('a failed Stop for A cannot clear the newly selected B busy state', async () => {
    let rejectStop!: (error: Error) => void
    let entered!: () => void
    const pending = new Promise<void>(resolve => { entered = resolve })

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'session.interrupt') { entered();

 return await new Promise<never>((_resolve, reject) => { rejectStop = reject }) }

      return {} as never
    })

    const active = { current: 'rt-abc123' as string | null }
    const selected = { current: 'rt-abc123' as string | null }
    let handle: HarnessHandle | null = null
    await actRender(<Harness activeSessionIdRef={active} onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} selectedStoredSessionIdRef={selected} />)
    const stop = handle!.cancelRunRaw()
    await pending
    active.current = 'B-runtime'
    selected.current = 'B-stored'
    $busy.set(true)
    rejectStop(new Error('A owner unavailable'))
    await stop
    expect($busy.get()).toBe(true)
    expect(active.current).toBe('B-runtime')
    $busy.set(false)
  })

  it('a write verdict learned during submit recovery prevents the pending send', async () => {
    const requestGateway = vi.fn(async (method: string) => method === 'session.resume'
      ? { session_id: 'recovered', writable: false, read_only_reason: 'session_read_only', info: { writable: false } } as never
      : {} as never)

    let handle: HarnessHandle | null = null
    await actRender(<Harness activeSessionId={null} onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} storedSessionId="import" />)
    expect(await handle!.submitText('must not dispatch after recovery')).toBe(false)
    expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toEqual([])
  })

  it('relay disconnect preserves observed execution and its pending controls, not a synthetic completion', async () => {
    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={async () => ({} as never)} />)
    await handle!.submitText('forwarded start')
    handle!.handleEvent({ type: 'message.start', session_id: 'rt-abc123', payload: {} })
    handle!.handleEvent({ type: 'session.info', session_id: 'rt-abc123', payload: { stored_session_id: 'rt-abc123', owner_connected: false, pending_submissions: [
      { admission_id: 'relay', input_id: 'forward', status: 'started', user: 'still owned', execution_generation: 3, controls: ['interrupt'] }
    ] } })
    expect(handle!.state().busy).toBe(true)
    expect(handle!.state().messages.some(message => message.completedAt)).toBe(false)
    expect(getQueuedPrompts('rt-abc123')).toMatchObject([{ id: 'relay', serverStatus: 'started', serverExecutionGeneration: 3 }])
  })

  it('keeps a slow submit owned past 30 seconds even if a queue drain fires', async () => {
    let accept!: (value: never) => void
    let entered!: () => void
    const started = new Promise<void>(resolve => { entered = resolve })

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'prompt.submit') { entered();

 return await new Promise<never>(resolve => { accept = resolve }) }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(<Harness onReady={h => { handle = h }} refreshSessions={async () => undefined} requestGateway={requestGateway} />)
    const first = handle!.submitTextRaw('slow A')
    await started
    const clock = vi.spyOn(Date, 'now').mockReturnValue(Date.now() + 31_000)

    try {
      expect(await handle!.submitTextRaw('B', { fromQueue: true })).toBe(false)
      expect(requestGateway.mock.calls.filter(([method]) => method === 'prompt.submit')).toHaveLength(1)
    } finally { clock.mockRestore(); accept({} as never) }

    expect(await first).toBe(true)
  })

  it.each([
    { status: 'queued', admission_id: 'owner-admission', forwarded: true },
    { status: 'queued', input_id: 'local-input' }
  ])('shows a legacy queued receipt as admitted, not a failed or running turn: %j', async receipt => {
    let latest: Record<string, unknown> | undefined

    // The legacy backend refuses `submission_id` (4000, pre-admission), then answers the identityless
    // retry with the owner's queued receipt: the prompt waits in the gateway's FIFO.
    const requestGateway = vi.fn(async (method: string, params?: Record<string, unknown>) => {
      if (method === 'prompt.submit' && params?.submission_id) {
        throw new JsonRpcGatewayError("unknown field 'submission_id'", { code: 4000 })
      }

      if (method === 'prompt.submit') {
        return receipt as never
      }

      return {} as never
    })

    let handle: HarnessHandle | null = null
    await actRender(
      <Harness
        onReady={h => (handle = h)}
        onSeedState={next => {
          latest = next
        }}
        refreshSessions={async () => undefined}
        requestGateway={requestGateway}
      />
    )

    expect(await handle!.submitText('my own question')).toBe(true)
    const messages = (latest?.messages ?? []) as { error?: string; role?: string }[]
    // No failed-turn card and no optimistic bubble: the queued-row card stands for the send.
    expect(messages.some(message => message.error)).toBe(false)
    expect(messages.some(message => message.role === 'user')).toBe(false)
    expect(latest?.busy).toBe(false)
    expect(latest?.awaitingResponse).toBe(false)
  })
})

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import { CanonicalDesktopProtocol } from '@/api/canonical-protocol'
import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import type { HermesGateway } from '@/hermes'
import { I18nProvider } from '@/i18n/context'
import { en } from '@/i18n/en'
import { $queuedPromptsBySession, enqueueQueuedPrompt, getQueuedPrompts } from '@/store/composer-queue'
import * as gatewayStore from '@/store/gateway'
import { reconcilePendingSubmissions } from '@/store/pending-submissions'
import { $activeGatewayProfile } from '@/store/profile'
import { $sessions } from '@/store/session'
import type { SessionInfo } from '@/types/hermes'

import { discardLostPrompt, refreshPendingPrompt } from './discard-lost-prompt'
import { renderComposerQueueSlot } from './queue-slot'

afterEach(() => { cleanup(); $queuedPromptsBySession.set({}); localStorage.clear(); $activeGatewayProfile.set('default') })

function renderSlot(
  request: (method: string, params?: Record<string, unknown>) => Promise<unknown> = vi.fn(async () => ({})),
  overrides: Partial<Parameters<typeof renderComposerQueueSlot>[0]> = {}
) {
  const inputs = {
    activeQueueSessionKey: 'stored',
    beginQueuedEdit: vi.fn(),
    busy: true,
    drainNextQueued: vi.fn(async () => undefined),
    exitQueuedEdit: vi.fn(),
    gateway: { request } as unknown as HermesGateway,
    queueEdit: null,
    queueParked: false,
    queuedPrompts: getQueuedPrompts('stored'),
    sendQueuedNow: vi.fn(),
    sessionId: 'runtime-1',
    steerQueuedNow: vi.fn(),
    t: en,
    ...overrides
  }

  render(
    <I18nProvider configClient={{ getConfig: async () => ({}), saveConfig: async () => ({ ok: true }) }}>
      {renderComposerQueueSlot(inputs)}
    </I18nProvider>
  )

  return inputs
}

async function expand() {
  fireEvent.click(await screen.findByRole('button', { name: /queued|message status/i }))
}

function rowButtons(text: string) {
  let row: HTMLElement | null = screen.getByText(text)

  while (row && !row.querySelector(`[aria-label="${en.composer.queueDelete}"]`)) {
    row = row.parentElement
  }

  return within(row as HTMLElement)
}

const labels = (row: ReturnType<typeof within>) =>
  row
    .queryAllByRole('button')
    .map((button: HTMLElement) => button.getAttribute('aria-label'))
    .filter(Boolean)

it('Delete on a server-queued card cancels its admission at the authority', async () => {
  reconcilePendingSubmissions('stored', [{ admission_id: 'adm-1', status: 'queued', user: 'later please' }])
  const request = vi.fn(async () => ({ admission_id: 'adm-1', status: 'terminal', outcome: 'cancelled' }))
  renderSlot(request)

  fireEvent.click(await screen.findByRole('button', { name: /queued|message status/i }))
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueDelete }))

  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('prompt.cancel', { session_id: 'runtime-1', admission_id: 'adm-1' }))
})

it('a server-queued card shows the Steer and Edit its owner serves, and never Next (RT-35)', async () => {
  reconcilePendingSubmissions('stored', [
    { admission_id: 'adm-1', status: 'queued', user: 'later please', controls: ['steer', 'replace'] }
  ])
  enqueueQueuedPrompt('stored', { text: 'local words', attachments: [] })
  renderSlot()

  await expand()
  expect(labels(rowButtons('later please')).sort()).toEqual(
    [en.composer.queueDelete, en.composer.queueEdit, en.composer.queueSteer].sort()
  )
  // The local row keeps its own controls, Next included.
  expect(labels(rowButtons('local words'))).toContain(en.composer.queueSendNext)
})

it('a server-queued card from an owner without the queued-row verbs keeps only Delete', async () => {
  reconcilePendingSubmissions('stored', [{ admission_id: 'adm-1', status: 'queued', user: 'later please' }])
  renderSlot()
  await expand()

  expect(labels(rowButtons('later please'))).toEqual([en.composer.queueDelete])
})

it('Steer on a server-queued card asks the authority to steer that admission', async () => {
  reconcilePendingSubmissions('stored', [
    { admission_id: 'adm-1', status: 'queued', user: 'later please', controls: ['steer', 'replace'] }
  ])
  const request = vi.fn(async () => ({ admission_id: 'adm-1', status: 'terminal', outcome: 'steered' }))
  const inputs = renderSlot(request)
  await expand()

  fireEvent.click(rowButtons('later please').getByRole('button', { name: en.composer.queueSteer }))

  await vi.waitFor(() =>
    expect(request).toHaveBeenCalledWith('prompt.steer_queued', { session_id: 'runtime-1', admission_id: 'adm-1' })
  )
  expect(inputs.steerQueuedNow).not.toHaveBeenCalled()
})

it('Edit on a server-queued card opens it in the composer', async () => {
  reconcilePendingSubmissions('stored', [
    { admission_id: 'adm-1', status: 'queued', user: 'later please', controls: ['steer', 'replace'] }
  ])
  const inputs = renderSlot()
  await expand()

  fireEvent.click(rowButtons('later please').getByRole('button', { name: en.composer.queueEdit }))

  expect(inputs.beginQueuedEdit).toHaveBeenCalledWith(expect.objectContaining({ id: 'adm-1', serverStatus: 'queued' }))
})

it('a forwarded started card stops only the observed admission generation', async () => {
  applyRuntimeInfo({ stored_session_id: 'stored', pending_submissions: [
    { admission_id: 'forwarded', input_id: 'input', status: 'started', user: 'forwarded work', execution_generation: 7, controls: ['interrupt'] }
  ] })
  const request = vi.fn(async () => ({}))
  renderSlot(request)
  await expand()
  fireEvent.click(screen.getByRole('button', { name: en.composer.stop }))
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('session.interrupt', { session_id: 'runtime-1', admission_id: 'forwarded', execution_generation: 7 }))
})

it('Refresh reads the forwarded receipt without resubmitting; the resulting unknown card uses Discard', async () => {
  applyRuntimeInfo({ stored_session_id: 'stored', pending_submissions: [
    { admission_id: 'forwarded', input_id: 'input', status: 'started', user: 'forwarded work', execution_generation: 7, controls: ['interrupt'] }
  ] })

  const request = vi.fn(async (method: string) => {
    if (method === 'prompt.receipt') {
      // The actual legacy handler emits this session.info projection after its owner read.
      applyRuntimeInfo({ stored_session_id: 'stored', pending_submissions: [
        { admission_id: 'forwarded', input_id: 'input', status: 'unknown', user: 'forwarded work', execution_generation: 7, controls: ['resolve_unknown'] }
      ] })
    }

    return {}
  })

  renderSlot(request)
  fireEvent.click(await screen.findByRole('button', { name: en.common.refresh }))
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('prompt.receipt', { session_id: 'runtime-1', admission_id: 'forwarded' }))
  cleanup()
  renderSlot(request)
  fireEvent.click(await screen.findByRole('button', { name: en.composer.queueLostDiscard }))
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('prompt.resolve_unknown', { session_id: 'runtime-1', admission_id: 'forwarded', execution_generation: 7 }))
  expect(request.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(false)
})

it('Discard on an observed barrier uses the exact owner admission/generation and live UI runtime', async () => {
  const sessionId = 'runtime-1'
  applyRuntimeInfo({ stored_session_id: 'stored', pending_submissions: [], blocking_unknown: [{
    admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'stored' },
    status: 'unknown', execution_generation: 12, controls: ['resolve_unknown']
  }] })
  enqueueQueuedPrompt('stored', { id: 'quiet-local', text: 'quiet local', attachments: [] })

  const request = vi.fn(async () => ({ admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'stored' },
    status: 'terminal', outcome: 'discarded', execution_generation: 12, authority_epoch: 2, sequence: 1 }))

  const inputs = renderSlot(request, { sessionId })
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('prompt.resolve_unknown', {
    session_id: sessionId, admission_id: 'other-viewer', execution_generation: 12
  }))
  await vi.waitFor(() => expect(getQueuedPrompts('stored').map(entry => entry.id)).toEqual(['quiet-local']))
  expect(inputs.sendQueuedNow).not.toHaveBeenCalled()
  expect(inputs.drainNextQueued).not.toHaveBeenCalled()
  expect(inputs.beginQueuedEdit).not.toHaveBeenCalled()
})

it('a delayed successful Discard cannot retire a newer observed generation', async () => {
  const listing = (generation: number) => ({ stored_session_id: 'stored', blocking_unknown: [{
    admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'stored' },
    status: 'unknown', execution_generation: generation, controls: ['resolve_unknown']
  }] })

  applyRuntimeInfo(listing(12))
  let finish!: (value: unknown) => void
  const request = vi.fn(() => new Promise<unknown>(resolve => { finish = resolve }))
  renderSlot(request)
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
  await vi.waitFor(() => expect(request).toHaveBeenCalled())
  applyRuntimeInfo(listing(13))
  finish({ admission_id: 'other-viewer', ref: { profile_id: 'default', session_id: 'stored' }, status: 'terminal', execution_generation: 12 })
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(getQueuedPrompts('stored')).toMatchObject([{ id: 'other-viewer', serverExecutionGeneration: 13 }])
})

it('an observed barrier cannot paint or control a different profile with a colliding session id', async () => {
  applyRuntimeInfo({ stored_session_id: 'stored', blocking_unknown: [{ admission_id: 'other-viewer',
    ref: { profile_id: 'default', session_id: 'stored' }, status: 'unknown', execution_generation: 12, controls: ['resolve_unknown'] }] })
  $activeGatewayProfile.set('other-profile')
  const request = vi.fn(async () => ({}))
  renderSlot(request)
  expect(screen.queryByRole('button', { name: en.composer.queueLostDiscard })).toBeNull()
  await expect(discardLostPrompt('runtime-1', 'stored', 'other-viewer', request as never, 12)).rejects.toThrow()
  expect(request).not.toHaveBeenCalled()
})

it('a stale observed card generation cannot send a Discard for its successor', async () => {
  const info = (generation: number) => ({ stored_session_id: 'stored', blocking_unknown: [{ admission_id: 'other-viewer',
    ref: { profile_id: 'default', session_id: 'stored' }, status: 'unknown', execution_generation: generation, controls: ['resolve_unknown'] }] })

  applyRuntimeInfo(info(12))
  applyRuntimeInfo(info(13))
  const request = vi.fn(async () => ({}))
  await expect(discardLostPrompt('runtime-1', 'stored', 'other-viewer', request as never, 12)).rejects.toThrow()
  expect(request).not.toHaveBeenCalled()
})

it('canonical resume unknown without a served Discard can only Refresh its metadata receipt', async () => {
  const protocol = new CanonicalDesktopProtocol()

  const resumed = protocol.result('session.resume', { session_id: 'stored' }, { session_id: 'stored', stored_session_id: 'stored',
    replay_epoch: 'epoch', last_sequence: 3, execution_generation: 15, pending: [{ admission_id: 'canonical-other',
      ref: { profile_id: 'default', session_id: 'stored' }, sequence: 1, status: 'unknown', outcome: null,
      authority_epoch: 1, execution_generation: 12, input_id: 'original-input', text: 'prior input', controls: [], steering: false }] })

  applyRuntimeInfo(resumed.info)

  const request = vi.fn(async () => ({ admission_id: 'canonical-other', ref: { profile_id: 'default', session_id: 'stored' },
    sequence: 1, status: 'unknown', outcome: null, authority_epoch: 1, execution_generation: 12, controls: ['resolve_unknown'] }))

  renderSlot(request, { sessionId: 'stored' })
  expect(screen.queryByRole('button', { name: en.composer.queueLostDiscard })).toBeNull()
  await refreshPendingPrompt('stored', 'stored', 'canonical-other', request as never)
  expect(getQueuedPrompts('stored')).toMatchObject([{ id: 'canonical-other', serverExecutionGeneration: 12, serverControls: ['resolve_unknown'] }])
})

it('a stale Discard callback cannot dispatch after fresh evidence removed that barrier', async () => {
  applyRuntimeInfo({ stored_session_id: 'stored', blocking_unknown: [{ admission_id: 'observed',
    ref: { profile_id: 'default', session_id: 'stored' }, status: 'unknown', execution_generation: 12, controls: ['resolve_unknown'] }] })
  const card = getQueuedPrompts('stored')[0]
  const request = vi.fn(async () => ({}))
  renderSlot(request)
  applyRuntimeInfo({ stored_session_id: 'stored', blocking_unknown: [] })
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
  await new Promise(resolve => setTimeout(resolve, 0))
  expect(request).not.toHaveBeenCalled()
  await expect(discardLostPrompt('runtime-1', 'stored', 'observed', request as never, 12, card)).rejects.toThrow()
})

it.each([
  { admission_id: 'wrong', ref: { profile_id: 'default', session_id: 'stored' }, status: 'terminal', execution_generation: 12 },
  { admission_id: 'observed', ref: { profile_id: 'other', session_id: 'stored' }, status: 'terminal', execution_generation: 12 },
  { admission_id: 'observed', ref: { profile_id: 'default', session_id: 'other' }, status: 'terminal', execution_generation: 12 },
  { admission_id: 'observed', ref: { profile_id: 'default', session_id: 'stored' }, status: 'terminal', execution_generation: 11 }
])('a mismatched Discard response cannot retire observed evidence: %j', async result => {
  applyRuntimeInfo({ stored_session_id: 'stored', blocking_unknown: [{ admission_id: 'observed',
    ref: { profile_id: 'default', session_id: 'stored' }, status: 'unknown', execution_generation: 12, controls: ['resolve_unknown'] }] })
  await discardLostPrompt('runtime-1', 'stored', 'observed', vi.fn(async () => result) as never, 12)
  expect(getQueuedPrompts('stored')).toMatchObject([{ id: 'observed', serverExecutionGeneration: 12 }])
})

it('observed Discard routes through the stored owner socket, not the ambient gateway', async () => {
  $sessions.set([{ id: 'stored', connection_id: 'owner-connection', profile: 'owner-profile' } as SessionInfo])

  const routed = vi.spyOn(gatewayStore, 'requestGatewayForAgent').mockResolvedValue({ admission_id: 'observed',
    ref: { profile_id: 'owner-profile', session_id: 'stored' }, status: 'terminal', execution_generation: 12 })

  const ambient = vi.fn(async () => ({}))

  try {
    applyRuntimeInfo({ stored_session_id: 'stored', blocking_unknown: [{ admission_id: 'observed',
      ref: { profile_id: 'owner-profile', session_id: 'stored' }, status: 'unknown', execution_generation: 12, controls: ['resolve_unknown'] }] })
    renderSlot(ambient)
    fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
    await vi.waitFor(() => expect(routed).toHaveBeenCalledWith('owner-connection', 'owner-profile', 'prompt.resolve_unknown', {
      session_id: 'runtime-1', admission_id: 'observed', execution_generation: 12
    }))
    expect(ambient).not.toHaveBeenCalled()
  } finally { routed.mockRestore(); $sessions.set([]) }
})

it('a forwarded unknown card discards the observed generation and never offers resend', async () => {
  applyRuntimeInfo({ stored_session_id: 'stored', pending_submissions: [
    { admission_id: 'forwarded', input_id: 'input', status: 'unknown', user: 'uncertain work', execution_generation: 8, controls: ['resolve_unknown'] }
  ] })
  const request = vi.fn(async () => ({}))
  const inputs = renderSlot(request)
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueLostDiscard }))
  await vi.waitFor(() => expect(request).toHaveBeenCalledWith('prompt.resolve_unknown', { session_id: 'runtime-1', admission_id: 'forwarded', execution_generation: 8 }))
  expect(inputs.sendQueuedNow).not.toHaveBeenCalled()
})

it.each([
  ['terminal', 'cancelled', 'backend_restart'],
  ['unknown', null, 'backend_restart_after_dispatch']
])('shows a local queue %s receipt after restart without resend or forwarded controls', async (status, outcome, reason) => {
  applyRuntimeInfo({ stored_session_id: 'stored', queue_receipts: [
    { input_id: 'local-B', session_id: 'stored', status, outcome, reason, user: 'accepted before restart' }
  ] })
  const request = vi.fn(async () => ({}))
  const inputs = renderSlot(request)
  expect(await screen.findByText(status === 'unknown' ? en.composer.queueRestartUnknown : en.composer.queueRestartCancelled)).toBeTruthy()
  expect(screen.getByText('accepted before restart')).toBeTruthy()
  expect(screen.queryByRole('button', { name: en.composer.queueLostDiscard })).toBeNull()
  expect(screen.queryByRole('button', { name: en.composer.queueSendNext })).toBeNull()
  expect(screen.queryByRole('button', { name: en.composer.queueEdit })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: en.composer.queueDelete }))
  expect(request).not.toHaveBeenCalled()
  expect(inputs.sendQueuedNow).not.toHaveBeenCalled()
  expect(getQueuedPrompts('stored')).toEqual([])
})

import { PassThrough } from 'stream'

import { renderSync } from '@hermes/ink'
import { stripAnsi } from '@hermes/shared/ansi'
import React from 'react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { createGatewayEventHandler } from '../app/createGatewayEventHandler.js'
import { turnController } from '../app/turnController.js'
import { getTurnState, resetTurnState } from '../app/turnStore.js'
import { resetUiState } from '../app/uiStore.js'
import { Md } from '../components/markdown.js'
import { acceptedCardAlt } from '../lib/cardWords.js'
import { DEFAULT_THEME } from '../theme.js'
import type { Msg } from '../types.js'

const ALT =
  'Airport to the hotel: YUL → Hôtel William Gray, 24 min by car, 21 km.\n' +
  'https://maps.apple.com/?saddr=45.4577,-73.7499&daddr=45.5075,-73.554&dirflg=d'

const CARD = { data: {}, kind: 'route', title: 'Airport to the hotel' }

const buildCtx = (appended: Msg[]) =>
  ({
    composer: { dequeue: () => undefined, queueEditRef: { current: null }, sendQueued: vi.fn(), setInput: vi.fn() },
    gateway: { gw: { request: vi.fn(async () => null) }, rpc: vi.fn(async () => null) },
    session: {
      STARTUP_RESUME_ID: '',
      colsRef: { current: 80 },
      newSession: vi.fn(),
      resetSession: vi.fn(),
      resumeById: vi.fn(),
      setCatalog: vi.fn()
    },
    submission: { submitRef: { current: vi.fn() } },
    system: { bellOnComplete: false, sys: vi.fn() },
    transcript: {
      appendMessage: (msg: Msg) => appended.push(msg),
      panel: vi.fn(),
      setHistoryItems: vi.fn()
    },
    voice: { setProcessing: vi.fn(), setRecording: vi.fn(), setVoiceEnabled: vi.fn() }
  }) as any

const cardTurn = (result: unknown, reply = 'It is 24 minutes by car; leave by 15:10.') => {
  const appended: Msg[] = []
  const onEvent = createGatewayEventHandler(buildCtx(appended))

  onEvent({ payload: {}, type: 'message.start' } as any)
  onEvent({ payload: { args: CARD, name: 'show_card', tool_id: 'call-1' }, type: 'tool.start' } as any)
  onEvent({ payload: { args: CARD, name: 'show_card', result, tool_id: 'call-1' }, type: 'tool.complete' } as any)
  onEvent({ payload: { text: reply }, type: 'message.complete' } as any)

  return appended
}

describe('Paper card words in the TUI', () => {
  beforeEach(() => {
    resetTurnState()
    resetUiState()
    turnController.fullReset()
  })

  it('shows an accepted card as its alt words, then the reply, with links intact', () => {
    const appended = cardTurn({ alt: ALT, card_id: 'call-1', ok: true, warnings: [] })
    const words = appended.findIndex(msg => msg.role === 'assistant' && msg.text === ALT)
    const reply = appended.findIndex(msg => msg.role === 'assistant' && msg.text.startsWith('It is 24 minutes'))

    expect(words).toBeGreaterThanOrEqual(0)
    expect(reply).toBeGreaterThan(words)
    expect(appended.some(msg => msg.text.includes('{"'))).toBe(false)
  })

  it('reads a receipt that arrives as its JSON text', () => {
    const appended = cardTurn(JSON.stringify({ alt: ALT, card_id: 'call-1', ok: true }))

    expect(appended.some(msg => msg.role === 'assistant' && msg.text === ALT)).toBe(true)
  })

  it('shows nothing for a refused card', () => {
    const appended = cardTurn({
      error: 'card refused: 1 problem',
      errors: [{ message: 'x', path: 'title' }],
      ok: false
    })

    expect(appended.filter(msg => msg.role === 'assistant').map(msg => msg.text)).toEqual([
      'It is 24 minutes by car; leave by 15:10.'
    ])
  })

  it('only reacts to show_card', () => {
    const appended: Msg[] = []
    const onEvent = createGatewayEventHandler(buildCtx(appended))

    onEvent({ payload: {}, type: 'message.start' } as any)
    onEvent({
      payload: { name: 'read_file', result: { alt: ALT, ok: true }, tool_id: 't' },
      type: 'tool.complete'
    } as any)

    expect(getTurnState().streamSegments.some(msg => msg.text === ALT)).toBe(false)
  })
})

describe('acceptedCardAlt', () => {
  it.each([
    [{ alt: ALT, ok: true }, ALT],
    [{ alt: ALT, ok: false }, null],
    [{ alt: '  ', ok: true }, null],
    [{ ok: true }, null],
    ['not json', null],
    [null, null],
    [42, null]
  ])('%j → %j', (result, words) => {
    expect(acceptedCardAlt(result)).toBe(words)
  })
})

const OSC = new RegExp(
  `${String.fromCharCode(27)}\\][\\s\\S]*?(?:${String.fromCharCode(7)}|${String.fromCharCode(27)}\\\\)`,
  'g'
)

const renderLines = (text: string) => {
  const stdout = Object.assign(new PassThrough(), { columns: 120, isTTY: false, rows: 24 })
  const stdin = Object.assign(new PassThrough(), { isTTY: false })
  const stderr = Object.assign(new PassThrough(), { isTTY: false })
  let output = ''

  stdout.on('data', chunk => {
    output += chunk.toString()
  })

  const instance = renderSync(React.createElement(Md, { cols: 120, t: DEFAULT_THEME, text }), {
    patchConsole: false,
    stderr: stderr as unknown as NodeJS.WriteStream,
    stdin: stdin as unknown as NodeJS.ReadStream,
    stdout: stdout as unknown as NodeJS.WriteStream
  })

  instance.unmount()
  instance.cleanup()

  return output
    .replace(OSC, '')
    .split('\n')
    .map(line => stripAnsi(line).trimEnd())
    .filter(Boolean)
}

it('renders the words line by line, the Maps link verbatim', () => {
  const [words, link] = ALT.split('\n')
  const lines = renderLines(ALT)

  expect(lines).toContain(words)
  expect(lines).toContain(link)
})

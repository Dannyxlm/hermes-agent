import { act, cleanup, waitFor } from '@testing-library/react'
import { createElement } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { $composerAttachments, type ComposerAttachment } from '@/store/composer'
import { $connection, $currentCwd } from '@/store/session'

import { actRender, Harness, type HarnessHandle } from './index-test-utils'

import { uploadComposerAttachment } from '.'

// Per-file copy, matching the convention of the other shards in this directory
// (preview-open.test.tsx, session-tile-actions.test.ts): the id the desktop
// holds is the *runtime* session id from session.create.
const RUNTIME_SESSION_ID = 'rt-abc123'

// Kept byte-identical to index.test.tsx: both specifiers are alias-based and
// this file sits in the same directory, so the mocks resolve to the same
// modules the parent suite mocks.
vi.mock('@/hermes', () => ({
  getProfiles: vi.fn(async () => ({ profiles: [] })),
  getSession: vi.fn(),
  PROMPT_SUBMIT_REQUEST_TIMEOUT_MS: 1_800_000,
  setApiRequestProfile: vi.fn(),
  transcribeAudio: vi.fn()
}))

vi.mock('@/store/gateway', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  requestGatewayForAgent: vi.fn()
}))

describe('uploadComposerAttachment remote read failures', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('turns the raw 16MB IPC cap error into a friendly remote-gateway message', async () => {
    // electron/hardening.ts rejects the readFileDataUrl IPC with this exact
    // shape when a file exceeds the configured data-URL read cap.
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: {
        readFileDataUrl: vi.fn(async () => {
          throw new Error('File preview failed: file is too large (20971520 bytes; limit 16777216 bytes).')
        })
      }
    })

    const requestGateway = vi.fn(async () => ({}) as never)

    await expect(
      uploadComposerAttachment(
        { id: 'file:big', kind: 'file', label: 'huge.csv', path: '/abs/huge.csv' },
        { remote: true, requestGateway, sessionId: RUNTIME_SESSION_ID }
      )
    ).rejects.toThrow(/huge\.csv.*16 MB/)

    // The cap is hit before any gateway round-trip.
    expect(requestGateway).not.toHaveBeenCalled()
  })
})

describe('uploadComposerAttachment image cache contract', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('always re-reads the image from disk, even when a stale previewUrl is cached', async () => {
    // attachImagePath drops `previewUrl` as soon as a thumbnail exists, so a
    // populated `previewUrl` here does not mean it holds full-resolution
    // bytes (#93324) — the upload must never trust it and must always read
    // the on-disk file for the bytes the model receives.
    const readFileDataUrl = vi.fn(async () => 'data:image/png;base64,ZnJvbS1kaXNr')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileDataUrl }
    })

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.attach_bytes') {
        return { attached: true, path: '/gw/images/shot.png' } as never
      }

      return {} as never
    })

    const uploaded = await uploadComposerAttachment(
      {
        id: 'image:shot.png',
        kind: 'image',
        label: 'shot.png',
        path: '/local/shot.png',
        previewUrl: 'data:image/png;base64,c3RhbGUtY2FjaGVk'
      },
      { remote: true, requestGateway, sessionId: RUNTIME_SESSION_ID }
    )

    expect(readFileDataUrl).toHaveBeenCalledWith('/local/shot.png')
    expect(requestGateway).toHaveBeenCalledWith('image.attach_bytes', {
      content_base64: 'ZnJvbS1kaXNr',
      filename: 'shot.png',
      session_id: RUNTIME_SESSION_ID
    })
    expect(uploaded.path).toBe('/gw/images/shot.png')
  })
})

describe('uploadComposerAttachment local cross-platform staging', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('uses image.attach_bytes for a Windows image when the local backend cwd is POSIX', async () => {
    const readFileDataUrl = vi.fn(async () => 'data:image/jpeg;base64,aGVsbG8=')
    Object.defineProperty(window, 'hermesDesktop', {
      configurable: true,
      value: { readFileDataUrl }
    })

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'image.attach_bytes') {
        return { attached: true, path: '/root/tmp/photo.jpg' } as never
      }

      return {} as never
    })

    const uploaded = await uploadComposerAttachment(
      {
        id: 'image:photo.jpg',
        kind: 'image',
        label: 'photo.jpg',
        path: 'C:\\Users\\alice\\Pictures\\photo.jpg'
      },
      {
        backendCwd: '/root',
        remote: false,
        requestGateway,
        sessionId: RUNTIME_SESSION_ID
      }
    )

    expect(readFileDataUrl).toHaveBeenCalledWith('C:\\Users\\alice\\Pictures\\photo.jpg')
    expect(requestGateway).toHaveBeenCalledWith('image.attach_bytes', {
      content_base64: 'aGVsbG8=',
      filename: 'photo.jpg',
      session_id: RUNTIME_SESSION_ID
    })
    expect(requestGateway).not.toHaveBeenCalledWith('image.attach', expect.anything())
    expect(uploaded.path).toBe('/root/tmp/photo.jpg')
  })
})

function deferred<T>() {
  let resolve!: (value: T) => void
  let reject!: (reason: Error) => void
  const promise = new Promise<T>((yes, no) => { resolve = yes; reject = no })

  return { promise, resolve, reject }
}

describe('eager upload occurrence ownership through usePromptActions', () => {
  afterEach(() => { cleanup(); $composerAttachments.set([]) })

  it.each(['success', 'failure'] as const)('late A %s cannot patch re-added B or make submit join A', async outcome => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/root')
    const a = deferred<{ attached: boolean; ref_text: string }>()
    const b = deferred<{ attached: boolean; ref_text: string }>()
    let uploads = 0

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'file.attach') { return await (++uploads === 1 ? a.promise : b.promise) as never }

      return {} as never
    })

    const chip: ComposerAttachment = { id: 'same-path', occurrenceId: 'A', kind: 'file', path: '/root/a.txt', label: 'a.txt' }
    $composerAttachments.set([chip])
    let handle: HarnessHandle | null = null
    await actRender(createElement(Harness, { onReady: h => { handle = h }, refreshSessions: async () => undefined, requestGateway }))
    await waitFor(() => expect(uploads).toBe(1))
    await act(async () => { $composerAttachments.set([]) })
    await act(async () => { $composerAttachments.set([{ ...chip, occurrenceId: 'B' }]) })
    // B must have its own flight while A remains pending.
    await waitFor(() => expect(uploads).toBe(2))
    await act(async () => { b.resolve({ attached: true, ref_text: 'B-ref' }) })
    await waitFor(() => expect($composerAttachments.get()[0]?.refText).toBe('B-ref'))
    const pendingA = $composerAttachments.get()[0]
    await act(async () => {
      if (outcome === 'success') { a.resolve({ attached: true, ref_text: 'A-ref' }) }
      else { a.reject(new Error('A failed')) }
    })
    expect($composerAttachments.get()[0]).toEqual(pendingA)
    expect(await handle!.submitText('send B')).toBe(true)
    expect(uploads).toBe(2)
    const send = requestGateway.mock.calls.find(([method]) => method === 'prompt.submit')
    expect(send).toBeDefined()
    expect($composerAttachments.get()).toEqual([])
  })

  it('submit for staged B does not join removed A’s still-pending flight', async () => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/root')
    const a = deferred<{ attached: boolean; ref_text: string }>()
    const requestGateway = vi.fn(async (method: string) => method === 'file.attach' ? await a.promise as never : {} as never)
    const chip: ComposerAttachment = { id: 'same', occurrenceId: 'A', kind: 'file', path: '/root/a.txt', label: 'a.txt' }
    $composerAttachments.set([chip])
    let handle: HarnessHandle | null = null
    await actRender(createElement(Harness, { onReady: h => { handle = h }, refreshSessions: async () => undefined, requestGateway }))
    await waitFor(() => expect(requestGateway).toHaveBeenCalledWith('file.attach', expect.anything()))
    await act(async () => { $composerAttachments.set([{ ...chip, occurrenceId: 'B', attachedSessionId: RUNTIME_SESSION_ID, refText: 'B-ref' }]) })
    const send = handle!.submitTextRaw('B now')

    try {
      await waitFor(() => expect(requestGateway.mock.calls.some(([method]) => method === 'prompt.submit')).toBe(true))
    } finally { a.resolve({ attached: true, ref_text: 'A-ref' }) }

    expect(await send).toBe(true)
    expect(requestGateway.mock.calls.filter(([method]) => method === 'file.attach')).toHaveLength(1)
  })

  it('a current occurrence failure stays visible and retries only on explicit submit', async () => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/root')
    let uploads = 0

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'file.attach') {
        if (++uploads === 1) { throw new Error('current upload failed') }

        return { attached: true, ref_text: 'recovered-ref' } as never
      }

      return {} as never
    })

    $composerAttachments.set([{ id: 'current-error', occurrenceId: 'C', kind: 'file', path: '/root/c.txt', label: 'c.txt' }])
    let handle: HarnessHandle | null = null
    await actRender(createElement(Harness, { onReady: h => { handle = h }, refreshSessions: async () => undefined, requestGateway }))
    await waitFor(() => expect($composerAttachments.get()[0]?.uploadState).toBe('error'))
    expect(uploads).toBe(1)
    expect(await handle!.submitText('retry current')).toBe(true)
    expect(uploads).toBe(2)
  })

  it('joins the current occurrence flight once without a second upload', async () => {
    $connection.set({ mode: 'local' } as never)
    $currentCwd.set('/root')
    const upload = deferred<{ attached: boolean; ref_text: string }>()
    let uploads = 0

    const requestGateway = vi.fn(async (method: string) => {
      if (method === 'file.attach') { uploads++;

 return await upload.promise as never }

      return {} as never
    })

    $composerAttachments.set([{ id: 'current', occurrenceId: 'C', kind: 'file', path: '/root/c.txt', label: 'c.txt' }])
    let handle: HarnessHandle | null = null
    await actRender(createElement(Harness, { onReady: h => { handle = h }, refreshSessions: async () => undefined, requestGateway }))
    await waitFor(() => expect(uploads).toBe(1))
    const send = handle!.submitTextRaw('send C')
    await act(async () => { upload.resolve({ attached: true, ref_text: 'C-ref' }) })
    expect(await send).toBe(true)
    expect(uploads).toBe(1)
  })
})

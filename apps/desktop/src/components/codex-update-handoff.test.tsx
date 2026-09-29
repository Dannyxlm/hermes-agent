import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus } from '@/global'
import { en } from '@/i18n/en'

import { CodexUpdateHandoff } from './codex-update-handoff'

const status: DesktopUpdateStatus = {
  supported: true,
  upstreamTracking: {
    ahead: 2,
    behind: 4,
    branch: 'main',
    checkedAt: 1,
    fetchedAt: 1,
    error: null,
    identityDirty: false,
    identitySource: 'install-stamp',
    installedRepository: 'Dannyxlm/hermes-agent',
    installedSha: 'a'.repeat(40),
    message: null,
    readOnly: true,
    repository: 'NousResearch/hermes-agent',
    state: 'ready',
    targetSha: 'b'.repeat(40),
    trackingRef: 'refs/test'
  }
}

afterEach(() => {
  cleanup()
  Reflect.deleteProperty(window, 'hermesDesktop')
})

it('copies version context without applying or claiming the request was sent', async () => {
  const writeClipboard = vi.fn().mockResolvedValue(undefined)
  const apply = vi.fn()
  window.hermesDesktop = { writeClipboard, updates: { apply } } as unknown as Window['hermesDesktop']
  render(<CodexUpdateHandoff status={status} />)
  expect(writeClipboard).not.toHaveBeenCalled()
  fireEvent.click(screen.getByRole('button', { name: en.updates.codexUpdateAction }))
  await waitFor(() => expect(screen.getByText(en.updates.codexUpdateCopied)).toBeTruthy())
  expect(writeClipboard).toHaveBeenCalledWith(expect.stringContaining(status.upstreamTracking!.installedSha!))
  expect(writeClipboard).toHaveBeenCalledWith(expect.stringContaining('$ava-hermes-updater'))
  expect(apply).not.toHaveBeenCalled()
})

it('reports a clipboard failure and lets the user retry without a false success', async () => {
  const writeClipboard = vi.fn().mockRejectedValueOnce(new Error('denied')).mockResolvedValue(undefined)
  window.hermesDesktop = { writeClipboard } as unknown as Window['hermesDesktop']
  render(<CodexUpdateHandoff status={status} />)
  fireEvent.click(screen.getByRole('button', { name: en.updates.codexUpdateAction }))
  await waitFor(() => expect(screen.getByRole('button', { name: en.common.copyFailed })).toBeTruthy())
  expect(screen.queryByText(en.updates.codexUpdateCopied)).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: en.common.copyFailed }))
  await waitFor(() => expect(screen.getByText(en.updates.codexUpdateCopied)).toBeTruthy())
})

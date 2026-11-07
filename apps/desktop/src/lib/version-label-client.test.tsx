import { act, cleanup, renderHook } from '@testing-library/react'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router'
import { afterEach, describe, expect, it } from 'vitest'

import { useUpdateVersionLabel } from '@/app/command-palette/use-update-version-label'
import { useStatusbarItems } from '@/app/shell/hooks/use-statusbar-items'
import type { DesktopUpstreamTracking, DesktopVersionInfo } from '@/global'
import { en } from '@/i18n/en'
import { $connection } from '@/store/session'
import { $backendUpdateStatus, $desktopVersion, $updateApply, $updateStatus } from '@/store/updates'

const packageCommit = 'abcdef1234567890abcdef1234567890abcdef12'
const runtimeCommit = '1234567890abcdef1234567890abcdef12345678'

const packageVersion: DesktopVersionInfo = {
  appVersion: '9.9.9+90.g1234567',
  backendVersion: '9.9.9+90.g1234567',
  clientVersion: '1.2.3+10.gabcdef1',
  commit: packageCommit,
  payload: 'bootstrap',
  electronVersion: 'test',
  nodeVersion: 'test',
  platform: 'linux',
  hermesRoot: '/isolated/runtime'
}

const tracking: DesktopUpstreamTracking = {
  ahead: 0,
  behind: 3,
  branch: 'main',
  checkedAt: 1,
  error: null,
  fetchedAt: 1,
  identityDirty: false,
  identitySource: 'checkout-head',
  installedRepository: 'example/hermes-agent',
  installedSha: runtimeCommit,
  message: null,
  readOnly: true,
  repository: 'NousResearch/hermes-agent',
  state: 'ready',
  targetSha: 'f'.repeat(40),
  trackingRef: 'refs/remotes/origin/main'
}

const idleApply = $updateApply.get()
const wrapper = ({ children }: { children: ReactNode }) => <MemoryRouter>{children}</MemoryRouter>

function mountClientBadge() {
  const hook = renderHook(
    () =>
      useStatusbarItems({
        agentsOpen: false,
        changeSessionCwd: async () => {},
        chatOpen: true,
        commandCenterOpen: false,
        extraLeftItems: [],
        extraRightItems: [],
        freshDraftReady: false,
        gatewayState: 'closed',
        inferenceStatus: null,
        openAgents: () => {},
        openCommandCenterSection: () => {},
        requestGateway: async () => {
          throw new Error('offline')
        },
        statusSnapshot: null,
        toggleCommandCenter: () => {}
      }),
    { wrapper }
  )

  return () => hook.result.current.statusbarItems.find(item => item.id === 'version-client')
}

afterEach(() => {
  cleanup()
  $connection.set(null)
  $desktopVersion.set(null)
  $updateStatus.set(null)
  $backendUpdateStatus.set(null)
  $updateApply.set(idleApply)
})

describe('client badge build identity', () => {
  it('keeps the package version and commit while the runtime checkout advances', () => {
    $desktopVersion.set(packageVersion)
    $updateStatus.set({ supported: true, currentSha: runtimeCommit, upstreamTracking: tracking })
    const badge = mountClientBadge()
    const palette = renderHook(() => useUpdateVersionLabel())
    expect(palette.result.current).toBe('v1.2.3+10')

    expect(badge()?.label).toBe('v1.2.3+10 (+3)')
    expect(badge()?.title).toContain(packageCommit.slice(0, 7))
    expect(badge()?.title).not.toContain(runtimeCommit.slice(0, 7))

    act(() =>
      $updateStatus.set({
        supported: true,
        currentSha: 'e'.repeat(40),
        upstreamTracking: { ...tracking, installedSha: 'e'.repeat(40), behind: 2, state: 'stale' }
      })
    )
    expect(badge()?.label).toBe('v1.2.3+10 (+2?)')
    expect(badge()?.title).toContain(packageCommit.slice(0, 7))
    expect(badge()?.title).not.toContain('eeeeeee')
    expect(palette.result.current).toBe('v1.2.3+10')

    act(() => $desktopVersion.set({ ...packageVersion, commit: 'b'.repeat(40) }))
    expect(badge()?.title).toContain('bbbbbbb')
    act(() => $desktopVersion.set({ ...packageVersion, clientVersion: '1.2.4' }))
    expect(palette.result.current).toBe('v1.2.4')
    act(() => $desktopVersion.set({ ...packageVersion, clientVersion: '', commit: 'b'.repeat(40) }))
    expect(palette.result.current).toBe('bbbbbbb')
    act(() => $desktopVersion.set({ ...packageVersion, clientVersion: '', commit: 'c'.repeat(40) }))
    expect(palette.result.current).toBe('ccccccc')
  })

  it('falls back to the build commit offline, never an updater SHA or a remote version', () => {
    $desktopVersion.set({ ...packageVersion, clientVersion: '' })
    $updateStatus.set({ supported: true, currentSha: runtimeCommit })
    const badge = mountClientBadge()

    expect(badge()?.label).toBe(packageCommit.slice(0, 7))
    expect(badge()?.hidden).toBe(false)
    expect(badge()?.title).not.toContain('9.9.9')

    act(() => $desktopVersion.set({ ...packageVersion, clientVersion: '', commit: null }))
    expect(badge()?.hidden).toBe(true)
  })

  it('keeps the client identity on a remote connection and refreshes it when the package changes', () => {
    $desktopVersion.set(packageVersion)
    $backendUpdateStatus.set({ supported: true, currentVersion: '8.7.6', currentSha: runtimeCommit })
    $connection.set({
      baseUrl: 'https://remote.example',
      isFullscreen: false,
      mode: 'remote',
      nativeOverlayWidth: 0,
      token: '',
      wsUrl: 'wss://remote.example',
      logs: [],
      windowButtonPosition: null
    })
    const badge = mountClientBadge()
    const palette = renderHook(() => useUpdateVersionLabel())
    expect(palette.result.current).toBe(en.shell.statusbar.backendLabel('8.7.6'))

    expect(badge()?.label).toBe(en.shell.statusbar.clientLabel('1.2.3+10'))
    expect(badge()?.title).toContain(packageCommit.slice(0, 7))
    expect(badge()?.title).not.toContain('9.9.9')

    act(() => $desktopVersion.set({ ...packageVersion, clientVersion: '1.2.4' }))
    expect(badge()?.label).toBe(en.shell.statusbar.clientLabel('1.2.4'))
    expect(palette.result.current).toBe(en.shell.statusbar.backendLabel('8.7.6'))
  })
})

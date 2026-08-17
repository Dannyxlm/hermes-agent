import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { DesktopUpdateStatus, DesktopUpstreamTracking } from '@/global'

const storage = new Map<string, string>()

vi.mock('@/lib/storage', () => ({
  persistBoolean: (key: string, value: boolean) => {
    storage.set(key, String(value))
  },
  persistString: (key: string, value: null | string) => {
    if (value === null) {
      storage.delete(key)
    } else {
      storage.set(key, value)
    }
  },
  storedBoolean: (key: string, fallback: boolean) => {
    const value = storage.get(key)

    return value === undefined ? fallback : value === 'true'
  },
  storedString: (key: string) => storage.get(key) ?? null
}))

const notifySpy = vi.fn()
const dismissSpy = vi.fn()

vi.mock('@/store/notifications', () => ({
  notify: (...args: unknown[]) => notifySpy(...args),
  dismissNotification: (...args: unknown[]) => dismissSpy(...args)
}))

const checkHermesUpdateSpy = vi.fn()
const updateHermesSpy = vi.fn()
const getActionStatusSpy = vi.fn()

vi.mock('@/hermes', () => ({
  checkHermesUpdate: (...args: unknown[]) => checkHermesUpdateSpy(...args),
  updateHermes: (...args: unknown[]) => updateHermesSpy(...args),
  getActionStatus: (...args: unknown[]) => getActionStatusSpy(...args)
}))

const {
  maybeNotifyUpdateAvailable,
  checkBackendUpdates,
  checkUpdates,
  $backendUpdateStatus,
  applyBackendUpdate,
  $backendUpdateApply,
  reportBackendContract,
  applyUpdates,
  $updateApply,
  $updateOverlayOpen,
  $updateOverlayTarget,
  requestActiveUpdate,
  $upstreamTrackingChecking,
  checkUpstreamTracking,
  openUpdateOverlayFor,
  resetUpdateApplyState,
  startUpdatePoller,
  stopUpdatePoller,
  $updateStatus
} = await import('./updates')

const { setConnection } = await import('./session')

const status = (over: Partial<DesktopUpdateStatus> = {}): DesktopUpdateStatus => ({
  supported: true,
  behind: 3,
  targetSha: 'sha-a',
  fetchedAt: 0,
  ...over
})

const lastToast = () => notifySpy.mock.calls.at(-1)?.[0] as { onDismiss: () => void }

const setRemote = (on: boolean) =>
  setConnection({
    baseUrl: 'http://box:9119',
    isFullscreen: false,
    mode: on ? 'remote' : 'local',
    nativeOverlayWidth: 0,
    token: 't',
    wsUrl: 'ws://box:9119',
    logs: [],
    windowButtonPosition: null
  })

describe('maybeNotifyUpdateAvailable', () => {
  beforeEach(() => {
    storage.clear()
    notifySpy.mockClear()
    vi.useRealTimers()
  })

  it('shows when an update is available and not snoozed', () => {
    maybeNotifyUpdateAvailable(status())
    expect(notifySpy).toHaveBeenCalledTimes(1)
    expect(notifySpy.mock.calls[0]?.[0]).toMatchObject({ icon: 'gift' })
  })

  it('stays quiet for new commits once the toast was closed', () => {
    maybeNotifyUpdateAvailable(status())
    lastToast().onDismiss() // user closes it → cooldown starts
    notifySpy.mockClear()

    // A different commit lands while still within the cooldown window.
    maybeNotifyUpdateAvailable(status({ targetSha: 'sha-b', behind: 9 }))
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('re-shows once the cooldown elapses', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)

    maybeNotifyUpdateAvailable(status())
    lastToast().onDismiss()
    notifySpy.mockClear()

    vi.setSystemTime(25 * 60 * 60 * 1000) // > 24h cooldown
    maybeNotifyUpdateAvailable(status({ targetSha: 'sha-b' }))
    expect(notifySpy).toHaveBeenCalledTimes(1)
  })

  it('does nothing when already up to date', () => {
    maybeNotifyUpdateAvailable(status({ behind: 0 }))
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('does not advertise a blocked packaged-app update', () => {
    maybeNotifyUpdateAvailable(status({ supported: false, behind: 1218, reason: 'fork-divergent' }))
    expect(notifySpy).not.toHaveBeenCalled()
  })

  // FAIL-BEFORE: a shallow installer clone reports behind:null + updateAvailable
  // (exact count unknowable without a merge-base). The guard treated null as 0
  // and silently swallowed the notification entirely.
  it('still notifies with generic copy when the exact behind count is unknown', () => {
    maybeNotifyUpdateAvailable(status({ behind: null, updateAvailable: true }))
    expect(notifySpy).toHaveBeenCalledTimes(1)
    expect(notifySpy.mock.calls[0]?.[0]).toMatchObject({ message: 'A new update is available.' })
  })
})

describe('reportBackendContract', () => {
  beforeEach(() => {
    storage.clear()
    notifySpy.mockClear()
    dismissSpy.mockClear()
    vi.useRealTimers()
  })

  it('dismisses the toast when the backend meets the contract', () => {
    reportBackendContract(6)
    expect(dismissSpy).toHaveBeenCalledWith('backend-contract-skew')
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('warns when the backend is behind (or reports no contract)', () => {
    reportBackendContract(undefined)
    expect(notifySpy).toHaveBeenCalledTimes(1)
    reportBackendContract(1)
    expect(notifySpy).toHaveBeenCalledTimes(2)
  })

  it('stays quiet on later session opens once the user closed it', () => {
    reportBackendContract(1)
    lastToast().onDismiss() // user closes it → cooldown starts
    notifySpy.mockClear()

    // Opening another pre-existing session re-runs the check within cooldown.
    reportBackendContract(1)
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('reminds again after the cooldown elapses', () => {
    vi.useFakeTimers()
    vi.setSystemTime(0)

    reportBackendContract(1)
    lastToast().onDismiss()
    notifySpy.mockClear()

    vi.setSystemTime(25 * 60 * 60 * 1000) // > 24h cooldown
    reportBackendContract(1)
    expect(notifySpy).toHaveBeenCalledTimes(1)
  })

  it('clears the snooze once the backend catches up, so a regression warns again', () => {
    reportBackendContract(1)
    lastToast().onDismiss()
    notifySpy.mockClear()

    reportBackendContract(6) // backend updated → satisfied, snooze cleared
    reportBackendContract(5) // a later regression must warn immediately
    expect(notifySpy).toHaveBeenCalledTimes(1)
  })
})

describe('checkBackendUpdates', () => {
  beforeEach(() => {
    storage.clear()
    notifySpy.mockClear()
    checkHermesUpdateSpy.mockReset()
    $backendUpdateStatus.set(null)
    vi.useRealTimers()
  })

  it('maps the backend /update/check onto the backend status, including commits', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'git',
      current_version: '0.16.0',
      behind: 2,
      update_available: true,
      can_apply: true,
      update_command: 'hermes update',
      message: null,
      commits: [{ sha: 'abc1234', summary: 'feat: x', author: 'a', at: 1 }]
    })

    const result = await checkBackendUpdates()

    expect(checkHermesUpdateSpy).toHaveBeenCalled()
    expect(result?.behind).toBe(2)
    expect(result?.updateAvailable).toBe(true)
    expect(result?.commits?.[0]?.sha).toBe('abc1234')
    expect(result?.supported).toBe(true)
    expect($backendUpdateStatus.get()?.commits?.[0]?.summary).toBe('feat: x')
  })

  it('preserves backend update_available when the backend cannot count commits', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'nixos',
      current_version: '0.16.0',
      behind: -1,
      update_available: true,
      can_apply: false,
      update_command: 'managed outside dashboard',
      message: 'Update available.'
    })

    const result = await checkBackendUpdates()

    expect(result?.behind).toBe(0)
    expect(result?.updateAvailable).toBe(true)
    expect(result?.targetSha).toBe('backend:0.16.0')
  })

  it('honours can_apply=false (docker/nix): not supported, carries message', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'docker',
      current_version: '0.16.0',
      behind: null,
      update_available: false,
      can_apply: false,
      update_command: 'docker pull ...',
      message: 'Docker images are immutable.'
    })

    const result = await checkBackendUpdates()

    expect(result?.supported).toBe(false)
    expect(result?.message).toBe('Docker images are immutable.')
  })

  it('maps managed immutable source status without hiding it as unsupported', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.19.0',
      behind: 4,
      update_available: true,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: '4 upstream commits are waiting for an immutable candidate.',
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        availability: 'ready',
        stale: false,
        status_error: null,
        count_basis: 'running_source',
        running_release: 'ava-converge-p1-f22a217b8dab',
        running_source: 'c'.repeat(40),
        running_upstream_base: 'a'.repeat(40),
        tracked_upstream: 'NousResearch/main',
        upstream_head: 'b'.repeat(40),
        commits_behind: 4,
        local_patch_count: 2,
        last_fetched_at: '2026-07-27T18:00:00+00:00',
        generated_at: '2026-07-27T18:00:00+00:00',
        candidate_status: 'not_built',
        blockers: [],
        next_action: 'Build an immutable candidate.',
        source_worktree_clean: true,
        source_refs_remotely_reachable: true,
        can_build_candidate: true,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: null
      }
    })

    const result = await checkBackendUpdates()

    expect(checkHermesUpdateSpy).toHaveBeenCalledWith(false)
    expect(result?.supported).toBe(true)
    expect(result?.behind).toBe(4)
    expect(result?.currentVersion).toBe('0.19.0')
    expect(result?.targetSha).toBe('b'.repeat(40))
    expect(result?.managedSource?.countBasis).toBe('running_source')
    expect(result?.managedSource?.runningSource).toBe('c'.repeat(40))
    expect(result?.managedSource?.candidateStatus).toBe('not_built')
    expect(result?.managedSource?.canBuildCandidate).toBe(true)
  })

  it('keeps non-ancestral managed forks honest instead of showing a false count', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.20.0',
      behind: null,
      update_available: false,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: 'A direct upstream commit count does not apply to this managed fork.',
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        availability: 'ready',
        stale: false,
        status_error: null,
        count_basis: 'unavailable_non_ancestral',
        running_release: 'hermes-v020',
        running_source: 'c'.repeat(40),
        running_upstream_base: 'a'.repeat(40),
        tracked_upstream: 'NousResearch/main',
        upstream_head: 'b'.repeat(40),
        running_source_is_ancestor_of_upstream: false,
        candidate_status: 'ready',
        candidate_target_revision: 'd'.repeat(40),
        candidate_target_is_ancestor_of_upstream: true,
        candidate_target_commits_behind: 17,
        local_patch_count: 3,
        last_fetched_at: '2026-08-04T02:00:00+00:00',
        generated_at: '2026-08-04T02:00:00+00:00',
        blockers: [],
        next_action: 'Activate or defer this exact immutable candidate.',
        source_worktree_clean: true,
        source_refs_remotely_reachable: true,
        can_build_candidate: false,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: null
      }
    })

    const result = await checkBackendUpdates()

    expect(result?.supported).toBe(true)
    expect(result?.behind).toBeUndefined()
    expect(result?.updateAvailable).toBe(false)
    expect(result?.targetSha).toBeUndefined()
    expect(result?.managedSource?.countBasis).toBe('unavailable_non_ancestral')
    expect(result?.managedSource?.runningSourceIsAncestorOfUpstream).toBe(false)
    expect(result?.managedSource?.candidateTargetRevision).toBe('d'.repeat(40))
    expect(result?.managedSource?.candidateTargetCommitsBehind).toBe(17)
  })

  it('rejects legacy or incomplete managed receipts instead of displaying a false exact count', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.19.0',
      behind: 575,
      update_available: true,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: 'Legacy source-monitor receipt.',
      managed_source: {
        schema_version: 'hermes-update-status.v1',
        availability: 'ready',
        stale: false,
        status_error: null,
        running_release: 'legacy-release',
        running_upstream_base: 'a'.repeat(40),
        tracked_upstream: 'NousResearch/main',
        upstream_head: 'b'.repeat(40),
        commits_behind: 575,
        local_patch_count: 2,
        last_fetched_at: '2026-07-27T18:00:00+00:00',
        generated_at: '2026-07-27T18:00:00+00:00',
        candidate_status: 'not_built',
        blockers: [],
        next_action: 'Build an immutable candidate.',
        source_worktree_clean: true,
        source_refs_remotely_reachable: true,
        can_build_candidate: true,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: null
      }
    })

    const result = await checkBackendUpdates()

    expect(result?.supported).toBe(true)
    expect(result?.behind).toBeUndefined()
    expect(result?.updateAvailable).toBe(false)
    expect(result?.targetSha).toBeUndefined()
    expect(result?.managedSource?.canBuildCandidate).toBe(false)
  })

  it('requests a managed refresh only for explicit Check now', async () => {
    setRemote(true)
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.19.0',
      behind: null,
      update_available: false,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: 'Update status is unavailable.',
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        availability: 'missing',
        stale: false,
        status_error: 'status_missing',
        can_build_candidate: false,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: { requested: true, error: null }
      }
    })

    await checkBackendUpdates(true)

    expect(checkHermesUpdateSpy).toHaveBeenCalledWith(true)
    expect($backendUpdateStatus.get()?.managedSource?.refreshRequest?.requested).toBe(true)
  })

  it('is a no-op in local mode (backend check only runs when remote)', async () => {
    setRemote(false)
    await checkBackendUpdates()
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
  })
})

// The ⌘K "Update Hermes" row. It used to call applyBackendUpdate() flat, which
// in local mode aimed at the backend checkout instead of the client and, with
// no overlay open, showed nothing at all.
describe('requestActiveUpdate', () => {
  const applyClientMock = vi.fn()
  const checkClientMock = vi.fn()

  beforeEach(() => {
    storage.clear()
    notifySpy.mockClear()
    dismissSpy.mockClear()
    applyClientMock.mockReset().mockResolvedValue({ ok: true, handedOff: true })
    checkClientMock.mockReset().mockResolvedValue(status({ behind: 0 }))
    updateHermesSpy.mockReset().mockResolvedValue({ ok: true, name: 'update' })
    checkHermesUpdateSpy.mockReset().mockResolvedValue({
      install_method: 'git',
      current_version: '0.4.2',
      behind: 0,
      update_available: false,
      can_apply: true,
      update_command: null,
      message: null
    })
    getActionStatusSpy.mockReset().mockResolvedValue({ lines: [], running: false, exit_code: 0 })
    resetUpdateApplyState()
    $updateStatus.set(null)
    $backendUpdateStatus.set(null)
    $updateOverlayOpen.set(false)
    ;(globalThis as unknown as { window: unknown }).window = {
      hermesDesktop: { updates: { apply: applyClientMock, check: checkClientMock } }
    }
    vi.useRealTimers()
  })

  afterEach(async () => {
    // Drain any backend apply this suite kicked off: applyBackendUpdate() now
    // memoizes the in-flight run, so a dangling promise here would be handed
    // to the next suite's tests instead of a fresh run.
    await vi.waitFor(() => expect($backendUpdateApply.get().applying).toBe(false), { timeout: 5000 })
    setRemote(false)
    delete (globalThis as unknown as { window?: unknown }).window
  })

  it('applies the CLIENT update in local mode, never the backend', async () => {
    setRemote(false)
    $updateStatus.set(status({ behind: 3 }))

    requestActiveUpdate()
    await vi.waitFor(() => expect(applyClientMock).toHaveBeenCalled())

    expect(updateHermesSpy).not.toHaveBeenCalled()
    expect($updateOverlayTarget.get()).toBe('client')
  })

  it('applies the BACKEND update in remote mode', async () => {
    setRemote(true)
    $backendUpdateStatus.set(status({ behind: 3 }))

    requestActiveUpdate()
    await vi.waitFor(() => expect(updateHermesSpy).toHaveBeenCalled())

    expect(applyClientMock).not.toHaveBeenCalled()
    expect($updateOverlayTarget.get()).toBe('backend')
  })

  it('always opens the overlay, so selecting the row is never a silent no-op', () => {
    setRemote(false)
    $updateStatus.set(status({ behind: 3 }))

    requestActiveUpdate()

    expect($updateOverlayOpen.get()).toBe(true)
  })

  it('opens the overlay to re-check instead of applying when already current', () => {
    setRemote(false)
    $updateStatus.set(status({ behind: 0, updateAvailable: false }))

    requestActiveUpdate()

    expect($updateOverlayOpen.get()).toBe(true)
    expect(applyClientMock).not.toHaveBeenCalled()
    expect(updateHermesSpy).not.toHaveBeenCalled()
  })

  it('applies on a backend that reports an update it cannot count commits for', async () => {
    setRemote(true)
    $backendUpdateStatus.set(status({ behind: 0, updateAvailable: true }))

    requestActiveUpdate()
    await vi.waitFor(() => expect(updateHermesSpy).toHaveBeenCalled())
  })
})

describe('desktop upstream tracking lane', () => {
  const checkMock = vi.fn()
  const checkUpstreamMock = vi.fn()

  const upstream = (overrides: Partial<DesktopUpstreamTracking> = {}): DesktopUpstreamTracking => ({
    ahead: 5,
    behind: 1218,
    branch: 'main',
    checkedAt: 1,
    error: null,
    fetchedAt: 1,
    identityDirty: false,
    identitySource: 'install-stamp',
    installedRepository: 'Dannyxlm/hermes-agent',
    installedSha: 'a'.repeat(40),
    message: null,
    readOnly: true,
    repository: 'NousResearch/hermes-agent',
    state: 'ready',
    targetSha: 'b'.repeat(40),
    trackingRef: 'refs/hermes-desktop-upstream/main',
    ...overrides
  })

  beforeEach(() => {
    checkMock.mockReset()
    checkUpstreamMock.mockReset()
    $updateStatus.set(status({ behind: 3, targetSha: 'publication-sha' }))
    $upstreamTrackingChecking.set(false)
    checkUpstreamMock.mockResolvedValue(upstream())
    ;(globalThis as unknown as { window: unknown }).window = {
      hermesDesktop: {
        updates: {
          check: checkMock,
          checkUpstream: checkUpstreamMock
        }
      }
    }
  })

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window
  })

  it('opens and refreshes the read-only surface without running the publication update check', async () => {
    openUpdateOverlayFor('client-upstream')

    await vi.waitFor(() => expect(checkUpstreamMock).toHaveBeenCalledTimes(1))

    expect(checkMock).not.toHaveBeenCalled()
    expect($updateStatus.get()?.behind).toBe(3)
    expect($updateStatus.get()?.targetSha).toBe('publication-sha')
    expect($updateStatus.get()?.upstreamTracking?.behind).toBe(1218)
  })

  it('updates only the nested upstream state when called directly', async () => {
    await checkUpstreamTracking()

    expect(checkUpstreamMock).toHaveBeenCalledTimes(1)
    expect(checkMock).not.toHaveBeenCalled()
    expect($updateStatus.get()?.supported).toBe(true)
    expect($updateStatus.get()?.upstreamTracking?.ahead).toBe(5)
  })

  it('does not let an older publication response overwrite a newer dedicated check', async () => {
    let resolvePublication: (value: DesktopUpdateStatus) => void = () => undefined

    const publicationResponse = new Promise<DesktopUpdateStatus>(resolve => {
      resolvePublication = resolve
    })

    checkMock.mockReturnValue(publicationResponse)
    checkUpstreamMock.mockResolvedValue(upstream({ checkedAt: 200, behind: 9 }))

    const publicationCheck = checkUpdates()
    await vi.waitFor(() => expect(checkMock).toHaveBeenCalledTimes(1))
    await checkUpstreamTracking()

    resolvePublication(
      status({
        upstreamTracking: upstream({ checkedAt: 100, behind: 12 })
      })
    )
    await publicationCheck

    expect($updateStatus.get()?.upstreamTracking?.checkedAt).toBe(200)
    expect($updateStatus.get()?.upstreamTracking?.behind).toBe(9)
  })

  it('does not let an older dedicated response overwrite a newer publication check', async () => {
    let resolveUpstream: (value: DesktopUpstreamTracking) => void = () => undefined

    const upstreamResponse = new Promise<DesktopUpstreamTracking>(resolve => {
      resolveUpstream = resolve
    })

    checkUpstreamMock.mockReturnValue(upstreamResponse)
    checkMock.mockResolvedValue(
      status({
        upstreamTracking: upstream({ checkedAt: 200, behind: 9 })
      })
    )

    const upstreamCheck = checkUpstreamTracking()
    await vi.waitFor(() => expect(checkUpstreamMock).toHaveBeenCalledTimes(1))
    await checkUpdates()

    resolveUpstream(upstream({ checkedAt: 100, behind: 12 }))
    await upstreamCheck

    expect($updateStatus.get()?.upstreamTracking?.checkedAt).toBe(200)
    expect($updateStatus.get()?.upstreamTracking?.behind).toBe(9)
  })

  it('does not let a delayed dedicated failure stale a newer publication success', async () => {
    let rejectUpstream: (error: Error) => void = () => undefined

    const upstreamResponse = new Promise<DesktopUpstreamTracking>((_resolve, reject) => {
      rejectUpstream = reject
    })

    checkUpstreamMock.mockReturnValue(upstreamResponse)
    checkMock.mockResolvedValue(
      status({
        upstreamTracking: upstream({ checkedAt: 200, behind: 9, state: 'ready' })
      })
    )

    const upstreamCheck = checkUpstreamTracking()
    await vi.waitFor(() => expect(checkUpstreamMock).toHaveBeenCalledTimes(1))
    await checkUpdates()

    rejectUpstream(new Error('offline'))
    await upstreamCheck

    expect($updateStatus.get()?.upstreamTracking?.checkedAt).toBe(200)
    expect($updateStatus.get()?.upstreamTracking?.behind).toBe(9)
    expect($updateStatus.get()?.upstreamTracking?.state).toBe('ready')
  })
})

describe('applyUpdates terminal state', () => {
  const applyMock = vi.fn()

  beforeEach(() => {
    storage.clear()
    notifySpy.mockClear()
    dismissSpy.mockClear()
    applyMock.mockReset()
    checkHermesUpdateSpy.mockReset()
    resetUpdateApplyState()
    $updateOverlayOpen.set(true)
    $updateStatus.set(null)
    $backendUpdateStatus.set(null)
    setRemote(false)
    ;(globalThis as unknown as { window: unknown }).window = {
      hermesDesktop: { updates: { apply: applyMock } }
    }
    vi.useRealTimers()
  })

  afterEach(() => {
    delete (globalThis as unknown as { window?: unknown }).window
  })

  it('holds the restart view when a relauncher hands off (no close, no toast)', async () => {
    applyMock.mockResolvedValue({ ok: true, handedOff: true })

    const result = await applyUpdates()

    expect(result.handedOff).toBe(true)
    // The detached relauncher will quit + reopen us; keep "applying" until then.
    expect($updateApply.get().applying).toBe(true)
    expect($updateOverlayOpen.get()).toBe(true)
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('waits for the matching Ava integration before applying a managed Desktop update', async () => {
    const clientTarget = 'a'.repeat(40)
    const cloudTarget = 'b'.repeat(40)
    setRemote(true)
    $updateStatus.set(status({ targetSha: clientTarget, updateAvailable: true }))
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.20.0',
      behind: 0,
      update_available: false,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: null,
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        count_basis: 'recorded_official_base',
        availability: 'ready',
        stale: false,
        status_error: null,
        running_source: cloudTarget,
        running_upstream_base: 'c'.repeat(40),
        upstream_head: 'c'.repeat(40),
        commits_behind: 0,
        local_patch_count: 8,
        can_build_candidate: false,
        candidate_request_available: false,
        refresh_request_available: false,
        refresh_request: null
      }
    })

    const result = await applyUpdates()

    expect(result).toMatchObject({ ok: false, error: 'paired-cloud-not-active' })
    expect(result.message).toContain('Update Ava first')
    expect(applyMock).not.toHaveBeenCalled()
  })

  it('fails closed without Ava provenance and serializes the async preflight', async () => {
    const clientTarget = 'a'.repeat(40)
    let resolveBackend: (value: unknown) => void = () => undefined
    const backendResponse = new Promise(resolve => {
      resolveBackend = resolve
    })
    setRemote(true)
    $updateStatus.set(status({ targetSha: clientTarget, updateAvailable: true }))
    checkHermesUpdateSpy.mockReturnValue(backendResponse)

    const first = applyUpdates()
    await vi.waitFor(() => expect(checkHermesUpdateSpy).toHaveBeenCalledTimes(1))

    const second = await applyUpdates()
    expect(second).toMatchObject({ ok: false, error: 'update-in-progress' })

    resolveBackend({
      install_method: 'managed-runtime',
      current_version: '0.20.0',
      behind: 0,
      update_available: false,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: null,
      managed_source: null
    })

    const result = await first
    expect(result).toMatchObject({ ok: false, error: 'paired-cloud-not-active' })
    expect(result.message).toContain("cannot verify Ava's active Hermes release")
    expect(applyMock).not.toHaveBeenCalled()
  })

  it('passes the exact Ava-matched publication SHA into Desktop apply', async () => {
    const target = 'a'.repeat(40)
    setRemote(true)
    $updateStatus.set(status({ targetSha: target, updateAvailable: true }))
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.20.0',
      behind: 0,
      update_available: false,
      can_apply: false,
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        count_basis: 'recorded_official_base',
        availability: 'ready',
        stale: false,
        running_source: target,
        upstream_head: target,
        commits_behind: 0
      }
    })
    applyMock.mockResolvedValue({ ok: true, handedOff: true })

    const result = await applyUpdates()

    expect(result.ok).toBe(true)
    expect(applyMock).toHaveBeenCalledWith({ expectedTargetSha: target })
  })

  it('closes the overlay + toasts when updated but not relaunched in place', async () => {
    // The Linux AppImage / dev-run path: backend + GUI updated, no in-place
    // relaunch. Must not strand the overlay on a closeless spinner.
    applyMock.mockResolvedValue({ ok: true, backendUpdated: true })

    await applyUpdates()

    expect($updateOverlayOpen.get()).toBe(false)
    expect($updateApply.get().applying).toBe(false)
    expect($updateApply.get().stage).toBe('idle')
    expect(notifySpy).toHaveBeenCalledTimes(1)
    expect(notifySpy.mock.calls[0]?.[0]).toMatchObject({ kind: 'success' })
  })

  it('lands on a closeable error state when the apply resolves not-ok', async () => {
    applyMock.mockResolvedValue({ ok: false, error: 'rebuild-failed', message: 'rebuild failed' })

    await applyUpdates()

    expect($updateApply.get().applying).toBe(false)
    expect($updateApply.get().stage).toBe('error')
    expect($updateApply.get().error).toBe('rebuild-failed')
  })

  it('preserves structured safe blockers for the close-and-update prompt', async () => {
    const blockers = [
      {
        pid: 47484,
        name: 'python.exe',
        cmdline: 'python.exe -m http.server 8766',
        kind: 'local-preview' as const,
        safeToStop: true,
        label: 'Example Preview',
        port: 8766
      }
    ]

    applyMock.mockResolvedValue({ ok: false, error: 'venv-blocked', message: 'blocked', blockers })

    await applyUpdates()

    expect($updateApply.get().error).toBe('venv-blocked')
    expect($updateApply.get().blockers).toEqual(blockers)
  })

  it('keeps the manual command state for CLI installs with no staged updater', async () => {
    applyMock.mockResolvedValue({ ok: true, manual: true, command: 'hermes update' })

    await applyUpdates()

    expect($updateApply.get().stage).toBe('manual')
    expect($updateApply.get().command).toBe('hermes update')
    expect($updateOverlayOpen.get()).toBe(true)
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('lands on the guiSkew terminal state for a GUI/backend skew (AppImage/.deb/.rpm), without claiming a GUI update', async () => {
    // Linux: backend updated, but the running desktop package was NOT replaced.
    // Must NOT toast "loads next launch" — that's the dishonest message #45205
    // guards against. Lands on a closeable guiSkew view instead.
    applyMock.mockResolvedValue({
      ok: true,
      backendUpdated: true,
      guiUpdated: false,
      guiSkew: true,
      message: 'Backend updated, but the desktop app package was not changed.'
    })

    const result = await applyUpdates()

    expect(result.guiUpdated).toBe(false)
    expect($updateApply.get().stage).toBe('guiSkew')
    expect($updateApply.get().applying).toBe(false)
    expect($updateApply.get().message).toMatch(/desktop app package was not changed/)
    // Overlay stays open on a closeable terminal view; no "all set" toast.
    expect($updateOverlayOpen.get()).toBe(true)
    expect(notifySpy).not.toHaveBeenCalled()
  })

  it('lands on a closeable manual-restart state when the rebuilt sandbox blocks auto-relaunch', async () => {
    // Under release/*-unpacked but chrome-sandbox isn't launchable: don't quit
    // into a dead app — keep a working window on a closeable manual state.
    applyMock.mockResolvedValue({
      ok: true,
      backendUpdated: true,
      guiUpdated: false,
      manualRestart: true,
      sandboxBlocked: true,
      message: 'Backend updated. Quit and reopen Hermes to finish.'
    })

    const result = await applyUpdates()

    expect(result.manualRestart).toBe(true)
    expect($updateApply.get().stage).toBe('manual')
    expect($updateApply.get().command).toBeNull()
    expect($updateApply.get().message).toMatch(/Quit and reopen/)
    expect($updateOverlayOpen.get()).toBe(true)
    expect(notifySpy).not.toHaveBeenCalled()
  })
})

describe('applyBackendUpdate recovery', () => {
  beforeEach(() => {
    storage.clear()
    checkHermesUpdateSpy.mockReset()
    updateHermesSpy.mockReset()
    getActionStatusSpy.mockReset()
    $backendUpdateStatus.set(null)
    $backendUpdateApply.set({
      applying: false,
      stage: 'idle',
      message: '',
      percent: null,
      error: null,
      command: null,
      log: []
    })
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('treats a managed candidate build as a request only and never polls for a restart', async () => {
    setConnection({
      baseUrl: 'http://box:9119',
      isFullscreen: false,
      mode: 'remote',
      nativeOverlayWidth: 0,
      token: 't',
      wsUrl: 'ws://box:9119',
      logs: [],
      windowButtonPosition: null
    })
    updateHermesSpy.mockResolvedValue({
      ok: true,
      name: 'hermes-update-candidate-request',
      pid: null,
      request_only: true,
      message: 'Immutable candidate build requested for bbbbbbb.'
    })
    checkHermesUpdateSpy.mockResolvedValue({
      install_method: 'managed-runtime',
      current_version: '0.19.0',
      behind: 4,
      update_available: true,
      can_apply: false,
      update_command: 'managed by immutable update train',
      message: 'Candidate request accepted.',
      managed_source: {
        schema_version: 'hermes-update-status.v2',
        availability: 'ready',
        stale: false,
        status_error: null,
        count_basis: 'running_source',
        running_source: 'c'.repeat(40),
        upstream_head: 'b'.repeat(40),
        commits_behind: 4,
        candidate_status: 'not_built',
        blockers: [],
        source_worktree_clean: true,
        source_refs_remotely_reachable: true,
        can_build_candidate: true,
        candidate_request_available: true,
        refresh_request_available: true,
        refresh_request: null
      }
    })

    const result = await applyBackendUpdate()

    expect(result.ok).toBe(true)
    expect(getActionStatusSpy).not.toHaveBeenCalled()
    expect(checkHermesUpdateSpy).toHaveBeenCalledWith(false)
    expect($backendUpdateApply.get().applying).toBe(false)
    expect($backendUpdateApply.get().message).toMatch(/requested/i)
  })

  it('waits for the backend to return after the restart drops the connection, then clears the overlay', async () => {
    const actionId = 'd'.repeat(32)
    updateHermesSpy.mockResolvedValue({ action_id: actionId, ok: true, name: 'update', pid: 1 })
    getActionStatusSpy.mockRejectedValueOnce(new Error('ECONNREFUSED')).mockResolvedValueOnce({
      exit_code: null,
      lines: [`=== hermes-update completed ${actionId} ===`],
      name: 'update',
      pid: null,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(5000)
    const result = await promise

    expect(result.ok).toBe(true)
    expect($backendUpdateApply.get().stage).toBe('idle')
    expect($backendUpdateApply.get().applying).toBe(false)
  })

  it('surfaces backend update action log lines while the action is running', async () => {
    const actionId = 'e'.repeat(32)
    updateHermesSpy.mockResolvedValue({ action_id: actionId, ok: true, name: 'update', pid: 1 })
    getActionStatusSpy
      .mockResolvedValueOnce({
        exit_code: null,
        lines: ['Pulling updates...', 'Installing dependencies...'],
        name: 'update',
        pid: 1,
        running: true
      })
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValueOnce({
        exit_code: null,
        lines: [`=== hermes-update completed ${actionId} ===`],
        name: 'update',
        pid: null,
        running: false
      })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)

    expect($backendUpdateApply.get().message).toBe('Installing dependencies...')
    expect($backendUpdateApply.get().log.map(entry => entry.message)).toEqual([
      'Pulling updates...',
      'Installing dependencies...'
    ])

    await vi.advanceTimersByTimeAsync(5000)
    await promise
  })

  it('keeps waiting past the old 45-second cutoff while the update action is running', async () => {
    const actionId = 'f'.repeat(32)
    updateHermesSpy.mockResolvedValue({ action_id: actionId, ok: true, name: 'hermes-update', pid: 1 })

    for (let attempt = 0; attempt < 31; attempt += 1) {
      getActionStatusSpy.mockResolvedValueOnce({
        exit_code: null,
        lines: ['=== hermes-update started now ===', `step ${attempt}`],
        name: 'hermes-update',
        pid: 1,
        running: true
      })
    }

    getActionStatusSpy.mockRejectedValueOnce(new Error('ECONNREFUSED')).mockResolvedValueOnce({
      exit_code: null,
      lines: [`=== hermes-update completed ${actionId} ===`],
      name: 'hermes-update',
      pid: null,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(46500)

    expect($backendUpdateApply.get().applying).toBe(true)
    expect($backendUpdateApply.get().stage).toBe('pull')

    await vi.advanceTimersByTimeAsync(5000)
    await expect(promise).resolves.toMatchObject({ ok: true })
  })

  it('treats a successful no-op as complete without waiting for a restart', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: 0,
      lines: ['stale output from another run', '=== hermes-update started now ===', '✓ Already up to date!'],
      name: 'hermes-update',
      pid: 1,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)
    const result = await promise

    expect(result.ok).toBe(true)
    expect($backendUpdateApply.get().stage).toBe('idle')
  })

  it('treats a successful dependency repair as complete without waiting for a restart', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: 0,
      lines: ['=== hermes-update started now ===', '✓ Dependencies repaired!', '✓ Update complete!'],
      name: 'hermes-update',
      pid: 1,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)
    await expect(promise).resolves.toMatchObject({ ok: true })
    expect($backendUpdateApply.get().stage).toBe('idle')
  })

  it('trusts the current action exit code without parsing its output', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: 0,
      lines: ['✓ Already up to date!'],
      name: 'hermes-update',
      pid: 1,
      running: false
    })
    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)
    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
  })

  it('waits for current-action completion proof after the backend restarts', async () => {
    const actionId = 'a'.repeat(32)
    updateHermesSpy.mockResolvedValue({ action_id: actionId, ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy
      .mockRejectedValueOnce(new Error('ECONNREFUSED'))
      .mockResolvedValueOnce({
        exit_code: null,
        lines: ['Update complete!', `=== hermes-update completed ${'c'.repeat(32)} ===`],
        name: 'hermes-update',
        pid: null,
        running: false
      })
      .mockResolvedValueOnce({
        exit_code: null,
        lines: ['Update complete!', `=== hermes-update completed ${actionId} ===`],
        name: 'hermes-update',
        pid: null,
        running: false
      })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(5000)
    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
  })

  it('accepts its terminal receipt when a verbose update pushes the start marker out of the log tail', async () => {
    const actionId = 'b'.repeat(32)
    updateHermesSpy.mockResolvedValue({ action_id: actionId, ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockRejectedValueOnce(new Error('ECONNREFUSED')).mockResolvedValueOnce({
      exit_code: null,
      lines: ['final build output', 'Update complete!', `=== hermes-update completed ${actionId} ===`],
      name: 'hermes-update',
      pid: null,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(5000)

    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(getActionStatusSpy).toHaveBeenCalledWith('hermes-update', 2000)
  })

  it('proves a pre-action-ID backend reached its requested commit after restart', async () => {
    $backendUpdateStatus.set({
      behind: 2,
      commits: [{ at: 1, author: 'Nous', sha: 'requested-target', summary: 'target' }],
      fetchedAt: 1,
      supported: true,
      targetSha: 'backend:0.18.2',
      updateAvailable: true
    })
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockRejectedValueOnce(new Error('ECONNREFUSED')).mockResolvedValue({
      exit_code: null,
      lines: ['verbose output', 'Update complete!'],
      name: 'hermes-update',
      pid: null,
      running: false
    })
    checkHermesUpdateSpy
      .mockResolvedValueOnce({
        behind: null,
        can_apply: true,
        commits: [],
        current_version: '0.18.2',
        install_method: 'git',
        message: 'offline',
        update_available: false,
        update_command: 'hermes update'
      })
      .mockResolvedValueOnce({
        behind: 1,
        can_apply: true,
        commits: [{ at: 2, author: 'Nous', sha: 'newer-commit', summary: 'newer' }],
        current_version: '0.18.2',
        install_method: 'git',
        message: null,
        update_available: true,
        update_command: 'hermes update'
      })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(5000)

    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(checkHermesUpdateSpy).toHaveBeenCalledTimes(2)
  })

  it('proves a fast pre-action-ID packaged update by its changed version', async () => {
    $backendUpdateStatus.set({
      behind: 1,
      commits: [],
      fetchedAt: 1,
      supported: true,
      targetSha: 'backend:0.18.2',
      updateAvailable: true
    })
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: null,
      lines: ['verbose output without a retained start marker'],
      name: 'hermes-update',
      pid: null,
      running: false
    })
    checkHermesUpdateSpy.mockResolvedValue({
      behind: -1,
      can_apply: true,
      commits: [],
      current_version: '0.18.3',
      install_method: 'pip',
      message: null,
      update_available: true,
      update_command: 'hermes update'
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)

    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(checkHermesUpdateSpy).toHaveBeenCalledWith(true)
  })

  it('resumes action polling after a transient status failure', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy
      .mockRejectedValueOnce(new Error('ECONNRESET'))
      .mockResolvedValueOnce({
        exit_code: null,
        lines: ['=== hermes-update started now ===', 'still running'],
        name: 'hermes-update',
        pid: 1,
        running: true
      })
      .mockResolvedValueOnce({
        exit_code: 0,
        lines: ['=== hermes-update started now ===', 'Update complete!'],
        name: 'hermes-update',
        pid: 1,
        running: false
      })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(5000)
    await expect(promise).resolves.toMatchObject({ ok: true })
    expect(getActionStatusSpy).toHaveBeenCalledTimes(3)
  })

  it('restores the fixed action deadline after reconnecting', async () => {
    updateHermesSpy.mockResolvedValue({ action_id: 'a'.repeat(32), ok: true, name: 'hermes-update', pid: 1 })

    const running = {
      exit_code: null,
      lines: ['still running'],
      name: 'hermes-update',
      pid: 1,
      running: true
    }

    for (let attempt = 0; attempt < 119; attempt += 1) {
      getActionStatusSpy.mockResolvedValueOnce(running)
    }

    getActionStatusSpy.mockRejectedValueOnce(new Error('ECONNRESET')).mockResolvedValue(running)

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(6 * 60 * 1000 + 1500)

    await expect(promise).resolves.toMatchObject({ error: 'apply-failed', ok: false })
    expect($backendUpdateApply.get().stage).toBe('error')
  })

  it('shares one in-flight update between concurrent apply requests', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: 0,
      lines: ['=== hermes-update started now ===', '✓ Already up to date!'],
      name: 'hermes-update',
      pid: 1,
      running: false
    })

    const first = applyBackendUpdate()
    const second = applyBackendUpdate()

    expect(second).toBe(first)
    await vi.advanceTimersByTimeAsync(1500)
    await Promise.all([first, second])
    expect(updateHermesSpy).toHaveBeenCalledTimes(1)
  })

  it('fails closed when the update action never reaches a terminal state', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: null,
      lines: ['=== hermes-update started now ===', 'still running'],
      name: 'hermes-update',
      pid: 1,
      running: true
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(6 * 60 * 1000 + 1500)
    await expect(promise).resolves.toMatchObject({ ok: false, error: 'apply-failed' })
    expect($backendUpdateApply.get().stage).toBe('error')
  })

  it('fails immediately when the update action exits nonzero', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'hermes-update', pid: 1 })
    getActionStatusSpy.mockResolvedValue({
      exit_code: 1,
      lines: ['=== hermes-update started now ===', 'update failed'],
      name: 'hermes-update',
      pid: 1,
      running: false
    })

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(1500)
    await expect(promise).resolves.toMatchObject({ ok: false, error: 'apply-failed' })
    expect(checkHermesUpdateSpy).not.toHaveBeenCalled()
    expect($backendUpdateApply.get().stage).toBe('error')
  })

  it('surfaces an error when the backend never comes back after the restart', async () => {
    updateHermesSpy.mockResolvedValue({ ok: true, name: 'update', pid: 1 })
    getActionStatusSpy.mockRejectedValue(new Error('ECONNREFUSED'))
    checkHermesUpdateSpy.mockRejectedValue(new Error('ECONNREFUSED'))

    const promise = applyBackendUpdate()
    await vi.advanceTimersByTimeAsync(250000)
    const result = await promise

    expect(result.ok).toBe(false)
    expect($backendUpdateApply.get().stage).toBe('error')
  }, 10000)
})

describe('startUpdatePoller', () => {
  const checkMock = vi.fn()
  const onProgressMock = vi.fn()
  const listeners: Record<string, Function> = {}

  beforeEach(() => {
    storage.clear()
    checkMock.mockReset()
    onProgressMock.mockReset()
    Object.keys(listeners).forEach(k => delete listeners[k])
    checkMock.mockResolvedValue({
      supported: true,
      behind: 5,
      targetSha: 'sha-abc',
      fetchedAt: 0
    })
    $updateStatus.set(null)
    ;(globalThis as unknown as { window: unknown }).window = {
      hermesDesktop: { updates: { check: checkMock, onProgress: onProgressMock } },
      addEventListener: vi.fn((event: string, handler: Function) => {
        listeners[event] = handler
      }),
      removeEventListener: vi.fn()
    }
    vi.useFakeTimers()
    stopUpdatePoller()
  })

  afterEach(() => {
    stopUpdatePoller()
    delete (globalThis as unknown as { window?: unknown }).window
    vi.useRealTimers()
  })

  it('calls checkUpdates() on startup so the version pill populates immediately', async () => {
    startUpdatePoller()

    // checkUpdates() is async — flush microtasks without advancing the 30-min interval.
    await vi.advanceTimersByTimeAsync(0)

    expect(checkMock).toHaveBeenCalled()
    expect($updateStatus.get()?.behind).toBe(5)
  })

  it('calls checkUpdates() on each interval tick', async () => {
    startUpdatePoller()
    await vi.advanceTimersByTimeAsync(0)
    checkMock.mockClear()

    await vi.advanceTimersByTimeAsync(30 * 60 * 1000)

    expect(checkMock).toHaveBeenCalled()
  })

  it('calls checkUpdates() when the window regains focus', async () => {
    startUpdatePoller()
    await vi.advanceTimersByTimeAsync(0)
    checkMock.mockClear()

    // Invoke the registered focus handler directly (the mock window doesn't
    // propagate DOM events, so call the stored listener).
    listeners['focus']?.()

    await vi.advanceTimersByTimeAsync(0)

    expect(checkMock).toHaveBeenCalled()
  })
})

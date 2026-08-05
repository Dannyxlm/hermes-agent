import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import type { DesktopUpdateStatus } from '@/global'
import {
  $backendUpdateApply,
  $updateApply,
  $updateOverlayOpen,
  $updateOverlayTarget,
  $updateStatus
} from '@/store/updates'

import { DesktopUpstreamTrackingView, ManagedSourceUpdateView, UpdatesOverlay } from './updates-overlay'

afterEach(() => cleanup())

function managedStatus(
  overrides: Partial<NonNullable<DesktopUpdateStatus['managedSource']>> = {}
): DesktopUpdateStatus {
  return {
    supported: true,
    behind: 4,
    updateAvailable: true,
    targetSha: 'b'.repeat(40),
    managedSource: {
      availability: 'ready',
      schemaVersion: 'hermes-update-status.v2',
      countBasis: 'recorded_official_base',
      stale: false,
      statusError: null,
      runningRelease: 'ava-converge-p1-f22a217b8dab',
      runningSource: 'c'.repeat(40),
      runningUpstreamBase: 'a'.repeat(40),
      trackedUpstream: 'NousResearch/main',
      upstreamHead: 'b'.repeat(40),
      commitsBehind: 4,
      localPatchCount: 2,
      overlayCount: 2,
      lastFetchedAt: '2026-07-27T18:00:00+00:00',
      generatedAt: '2026-07-27T18:00:00+00:00',
      candidateStatus: 'not_built',
      blockers: [],
      nextAction: 'Build an immutable candidate.',
      sourceWorktreeClean: true,
      sourceRefsRemotelyReachable: true,
      canBuildCandidate: true,
      candidateRequestAvailable: true,
      refreshRequestAvailable: true,
      refreshRequest: null,
      ...overrides
    }
  }
}

function renderManaged(ui: ReactNode) {
  return render(
    <Dialog open>
      <DialogContent>{ui}</DialogContent>
    </Dialog>
  )
}

function upstreamStatus(
  overrides: Partial<NonNullable<DesktopUpdateStatus['upstreamTracking']>> = {}
): DesktopUpdateStatus {
  return {
    supported: true,
    upstreamTracking: {
      ahead: 5,
      behind: 1197,
      branch: 'main',
      checkedAt: Date.now(),
      error: null,
      fetchedAt: Date.now(),
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
    }
  }
}

describe('DesktopUpstreamTrackingView', () => {
  it('shows packaged-app provenance and upstream drift without an apply action', () => {
    const onCheckNow = vi.fn()
    const onDone = vi.fn()

    renderManaged(
      <DesktopUpstreamTrackingView checking={false} onCheckNow={onCheckNow} onDone={onDone} status={upstreamStatus()} />
    )

    expect(screen.getByText('Desktop upstream')).toBeTruthy()
    expect(screen.getByText('1197')).toBeTruthy()
    expect(screen.getByText('5')).toBeTruthy()
    expect(screen.getByText(/Dannyxlm\/hermes-agent@aaaaaaaaaaaa/)).toBeTruthy()
    expect(screen.getByText(/never resets a checkout/)).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Update now' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))
    fireEvent.click(screen.getByRole('button', { name: 'Done' }))
    expect(onCheckNow).toHaveBeenCalledOnce()
    expect(onDone).toHaveBeenCalledOnce()
  })

  it('labels cached data stale instead of presenting its count as current', () => {
    renderManaged(
      <DesktopUpstreamTrackingView
        checking={false}
        onCheckNow={vi.fn()}
        onDone={vi.fn()}
        status={upstreamStatus({
          error: 'fetch-failed',
          fetchedAt: null,
          message: 'offline',
          state: 'stale'
        })}
      />
    )

    expect(screen.getByText('Showing cached official upstream status.')).toBeTruthy()
    expect(screen.getByText('offline')).toBeTruthy()
    expect(screen.getByText('1197')).toBeTruthy()
  })

  it('cannot inherit a failed app-update retry action', () => {
    $updateStatus.set(upstreamStatus())
    $updateApply.set({
      applying: false,
      command: null,
      error: 'apply-failed',
      log: [],
      message: 'A prior application update failed.',
      percent: null,
      stage: 'error'
    })
    $backendUpdateApply.set({
      applying: false,
      command: 'managed update',
      error: null,
      log: [],
      message: 'A backend update is waiting for manual completion.',
      percent: null,
      stage: 'manual'
    })
    $updateOverlayTarget.set('client-upstream')
    $updateOverlayOpen.set(true)

    render(<UpdatesOverlay />)

    expect(screen.getByText('Desktop upstream')).toBeTruthy()
    expect(screen.queryByText('A prior application update failed.')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Try again' })).toBeNull()
    expect(screen.queryByRole('button', { name: 'Update now' })).toBeNull()

    fireEvent.click(screen.getByRole('button', { name: 'Done' }))

    expect($updateOverlayOpen.get()).toBe(false)
    expect($updateApply.get().stage).toBe('error')
    expect($backendUpdateApply.get().stage).toBe('manual')
  })
})

describe('ManagedSourceUpdateView', () => {
  it('renders immutable source state and labels both request-only actions honestly', () => {
    const onCheckNow = vi.fn()
    const onBuildCandidate = vi.fn()

    renderManaged(
      <ManagedSourceUpdateView
        building={false}
        checking={false}
        onBuildCandidate={onBuildCandidate}
        onCheckNow={onCheckNow}
        status={managedStatus()}
      />
    )

    expect(screen.getByText('Immutable update train')).toBeTruthy()
    expect(screen.getByText(/4 upstream commits/)).toBeTruthy()
    expect(screen.getByText('Candidate')).toBeTruthy()
    expect(screen.getByText('Not built')).toBeTruthy()
    expect(screen.getByText(/Production is not changed or restarted/)).toBeTruthy()

    fireEvent.click(screen.getByRole('button', { name: 'Check now' }))
    fireEvent.click(screen.getByRole('button', { name: 'Build candidate' }))
    expect(onCheckNow).toHaveBeenCalledOnce()
    expect(onBuildCandidate).toHaveBeenCalledOnce()
  })

  it('shows stale and blocker states and disables unsafe candidate requests', () => {
    renderManaged(
      <ManagedSourceUpdateView
        building={false}
        checking={false}
        onBuildCandidate={vi.fn()}
        onCheckNow={vi.fn()}
        status={managedStatus({
          availability: 'stale',
          stale: true,
          canBuildCandidate: false,
          blockers: ['Source refs are not remotely reachable.'],
          nextAction: 'Publish the source refs, then check again.'
        })}
      />
    )

    expect(screen.getByText(/stale/i)).toBeTruthy()
    expect(screen.getByText('Source refs are not remotely reachable.')).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Build candidate' }) as HTMLButtonElement).disabled).toBe(true)
    expect((screen.getByRole('button', { name: 'Check now' }) as HTMLButtonElement).disabled).toBe(false)
  })

  it('keeps missing or invalid monitor state visible instead of calling it unsupported', () => {
    renderManaged(
      <ManagedSourceUpdateView
        building={false}
        checking={false}
        onBuildCandidate={vi.fn()}
        onCheckNow={vi.fn()}
        status={managedStatus({
          availability: 'invalid',
          schemaVersion: 'hermes-update-status.v1',
          statusError: 'status_schema_invalid',
          canBuildCandidate: false,
          candidateRequestAvailable: false,
          refreshRequestAvailable: true
        })}
      />
    )

    expect(screen.getByText(/status is invalid/i)).toBeTruthy()
    expect(screen.getAllByText('Unknown').length).toBeGreaterThanOrEqual(1)
    expect(screen.queryByText(/4 upstream commits/)).toBeNull()
    expect((screen.getByRole('button', { name: 'Check now' }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: 'Build candidate' }) as HTMLButtonElement).disabled).toBe(true)
  })

  it('does not relabel a legacy Git patch count as CloudSeed overlays', () => {
    renderManaged(
      <ManagedSourceUpdateView
        building={false}
        checking={false}
        onBuildCandidate={vi.fn()}
        onCheckNow={vi.fn()}
        status={managedStatus({ countBasis: 'running_source', overlayCount: undefined, localPatchCount: 46 })}
      />
    )

    expect(screen.getByText('CloudSeed overlays')).toBeTruthy()
    expect(screen.getByText('Unknown')).toBeTruthy()
    expect(screen.queryByText('46')).toBeNull()
  })
})

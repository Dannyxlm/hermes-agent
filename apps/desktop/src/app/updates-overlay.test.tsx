import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import type { ReactNode } from 'react'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { Dialog, DialogContent } from '@/components/ui/dialog'
import type { DesktopUpdateStatus } from '@/global'

import { ManagedSourceUpdateView } from './updates-overlay'

afterEach(() => cleanup())

function managedStatus(overrides: Partial<NonNullable<DesktopUpdateStatus['managedSource']>> = {}): DesktopUpdateStatus {
  return {
    supported: true,
    behind: 4,
    updateAvailable: true,
    targetSha: 'b'.repeat(40),
    managedSource: {
      availability: 'ready',
      stale: false,
      statusError: null,
      runningRelease: 'ava-converge-p1-f22a217b8dab',
      runningUpstreamBase: 'a'.repeat(40),
      trackedUpstream: 'NousResearch/main',
      upstreamHead: 'b'.repeat(40),
      commitsBehind: 4,
      localPatchCount: 2,
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
          statusError: 'status_schema_invalid',
          canBuildCandidate: false,
          candidateRequestAvailable: false,
          refreshRequestAvailable: true
        })}
      />
    )

    expect(screen.getByText(/status is invalid/i)).toBeTruthy()
    expect((screen.getByRole('button', { name: 'Check now' }) as HTMLButtonElement).disabled).toBe(false)
    expect((screen.getByRole('button', { name: 'Build candidate' }) as HTMLButtonElement).disabled).toBe(true)
  })
})

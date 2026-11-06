import { act, cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { familyTranslations } from '@/i18n/family'

import { familyTarget, type FamilyTarget } from './actions'
import { FamilyDialog } from './dialog'
import { type FamilyDisplayNode, familyRows, parseFamily } from './model'

const rpc = vi.hoisted(() => vi.fn())
const open = vi.hoisted(() => vi.fn())
const owner = vi.hoisted(() => vi.fn())
vi.mock('@/i18n', () => ({ useI18n: () => ({ t: { family: familyTranslations.en, common: { close: 'Close' } } }) }))
vi.mock('@/store/gateway', () => ({
  activeGateway: () => null,
  requestGatewayForAgent: (...args: unknown[]) => rpc(...args),
  requestGatewayForProfile: (...args: unknown[]) => rpc(...args),
  retainGatewayForSessionTurn: vi.fn()
}))
vi.mock('@/store/session-states', () => ({
  knownOwnerForSession: (...args: unknown[]) => owner(...args),
  ownerConnectionMode: () => 'remote',
  storedSessionIdForRuntimeId: (id: string) => id === 'runtime-parent' ? 'stored-parent' : null
}))
vi.mock('@/store/session-gone-latch', () => ({ resetBackgroundPollingGuardAfterRebind: vi.fn() }))
vi.mock('@/sdk', () => ({ host: { openSession: (...args: unknown[]) => open(...args) } }))

const target: FamilyTarget = { sessionId: 'stored-parent', owner: { connectionId: 'box', profile: 'desktop-alias', targetProfile: 'default', mode: 'remote' } }

function node(overrides: Partial<FamilyDisplayNode> = {}): FamilyDisplayNode {
  return { node_id: 'thread:child', parent_node_id: 'session:parent', profile: 'default', session_id: 'stored-child',
    title: 'Worker investigation', full_title: null, kind: 'thread', status: 'working', activity_text: 'Reading sources',
    actions: ['open'], needs_you: null, ...overrides }
}

function tree(nodes = [node()]) {
  return { protocol_version: 1, root: node({ node_id: 'session:parent', parent_node_id: null, title: 'Parent chat', session_id: 'stored-parent' }),
    nodes, batches: [], complete: true, unavailable_profiles: [], truncated: false, generated_at: 1 }
}

beforeEach(() => { vi.clearAllMocks(); open.mockResolvedValue(undefined); owner.mockReturnValue(target.owner); rpc.mockResolvedValue(tree()) })
afterEach(() => { cleanup(); vi.useRealTimers() })

describe('Family rendered native-contract browser', () => {
  it('reads the stored parent on its captured owner; opening a worker uses its stored identity and existing chat door', async () => {
    const onClose = vi.fn()
    render(<FamilyDialog onClose={onClose} target={target} />)
    expect(await screen.findByText('Worker investigation')).toBeTruthy()
    expect(rpc).toHaveBeenCalledWith('box', 'desktop-alias', 'session.family', { session_id: 'stored-parent', profile: 'default' }, 15_000, undefined)
    fireEvent.click(screen.getByRole('button', { name: 'Open chat' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('stored-child', expect.objectContaining({
      route: { connectionId: 'box', profile: 'desktop-alias', targetProfile: 'default', mode: 'remote' },
      intent: 'tab', forceResume: true, awaitHydration: true
    })))
    expect(onClose).toHaveBeenCalledTimes(1)
    expect(rpc.mock.calls.every(call => call[2] === 'session.family')).toBe(true)
  })

  it('offers only advertised actions, answering the exact needs-you chat rather than the parent', async () => {
    rpc.mockResolvedValue(tree([
      node({ status: 'needs_you', actions: ['answer'], needs_you: { kind: 'clarify', profile: 'reviewer', session_id: 'question-chat' } }),
      node({ node_id: 'helper:1', title: 'Read-only helper', session_id: null, actions: ['stop'], status: 'unknown' })
    ]))
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    expect(await screen.findByText('Unknown')).toBeTruthy()
    expect(screen.queryByRole('button', { name: 'Open chat' })).toBeNull()
    expect(screen.queryByRole('button', { name: /stop|resume/i })).toBeNull()
    expect(screen.getByText('No chat action available')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Answer in chat' }))
    await waitFor(() => expect(open).toHaveBeenCalledWith('question-chat', expect.objectContaining({
      route: { connectionId: 'box', profile: 'reviewer', targetProfile: 'reviewer', mode: 'remote' }
    })))
  })

  it('shows partial, truncated and unavailable-profile evidence explicitly', async () => {
    rpc.mockResolvedValue({ ...tree(), complete: false, truncated: true, unavailable_profiles: ['reviewer'] })
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    expect(await screen.findByText(/Some profiles are unavailable/)).toBeTruthy()
    expect(screen.getByText(/larger than the current window/)).toBeTruthy()
  })

  it('supports overview and drilldown without changing the owner connection', async () => {
    rpc.mockImplementation((_connection, _profile, method) => Promise.resolve(method === 'family.overview'
      ? { protocol_version: 1, complete: true, unavailable_profiles: [], roots: [{ root: node({ profile: 'reviewer', session_id: 'other-root', title: 'Other family' }) }], earlier: [] }
      : tree()))
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    await screen.findByText('Worker investigation')
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    await screen.findByText('Other family')
    fireEvent.click(screen.getByRole('button', { name: 'View family' }))
    await screen.findByText('Worker investigation')
    expect(rpc).toHaveBeenLastCalledWith('box', 'reviewer', 'session.family', { session_id: 'other-root', profile: 'reviewer' }, 15_000, undefined)
  })

  it('never paints a late previous tree under the newly selected overview', async () => {
    let settle: (value: unknown) => void = () => undefined
    rpc.mockReturnValueOnce(new Promise(resolve => { settle = resolve }))
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    fireEvent.click(screen.getByRole('button', { name: 'Overview' }))
    rpc.mockResolvedValue({ protocol_version: 1, complete: true, unavailable_profiles: [], roots: [], earlier: [] })
    // The overview request already started; retry against the ready fixture.
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('No related work in this window')).toBeTruthy()
    await act(async () => settle(tree([node({ title: 'Stale worker' })])))
    expect(screen.queryByText('Stale worker')).toBeNull()
    expect(screen.queryByRole('button', { name: 'Open chat' })).toBeNull()
  })

  it('distinguishes unsupported from a transient error and allows explicit retry', async () => {
    rpc.mockRejectedValueOnce(new Error('Method not found'))
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    expect(await screen.findByText('Family is unavailable on this backend.')).toBeTruthy()
    rpc.mockRejectedValueOnce(new Error('Connection lost'))
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Could not read Family')).toBeTruthy()
    expect(screen.getByText('Connection lost')).toBeTruthy()
    fireEvent.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Worker investigation')).toBeTruthy()
  })

  it('keeps failed opens visible and does not claim that an answer was sent', async () => {
    open.mockRejectedValueOnce(new Error('Session owner unavailable'))
    render(<FamilyDialog onClose={vi.fn()} target={target} />)
    fireEvent.click(await screen.findByRole('button', { name: 'Open chat' }))
    expect(await screen.findByRole('alert')).toHaveProperty('textContent', 'Session owner unavailable')
    expect(screen.getByRole('button', { name: 'Open chat' })).toBeTruthy()
  })

  it('closes with Escape and makes an unresolved owner explicit without ambient requests', async () => {
    const close = vi.fn()
    render(<FamilyDialog onClose={close} target={null} />)
    const dialog = screen.getByRole('dialog')
    expect(within(dialog).getByText(familyTranslations.en.noOwner)).toBeTruthy()
    expect(rpc).not.toHaveBeenCalled()
    fireEvent.keyDown(dialog, { key: 'Escape' })
    await waitFor(() => expect(close).toHaveBeenCalled())
  })
})

describe('Family identity and projection boundary', () => {
  it('translates a runtime selection to its stored parent before reading', () => {
    expect(familyTarget('runtime-parent')).toEqual(target)
    owner.mockReturnValue(undefined)
    expect(familyTarget('unowned')).toBeNull()
  })
  it('rejects malformed/unknown protocol data instead of inventing statuses or controls', () => {
    expect(() => parseFamily({ ...tree(), protocol_version: 2 }, false)).toThrow()
    expect(() => parseFamily(tree([node({ status: 'invented' as never })]), false)).toThrow()
    expect(() => parseFamily(tree([node({ actions: ['send' as never] })]), false)).toThrow()
  })
  it('keeps orphan and cyclic nodes readable with bounded indentation', () => {
    const rows = familyRows([node({ node_id: 'a', parent_node_id: 'b' }), node({ node_id: 'b', parent_node_id: 'a' }), node({ node_id: 'c', parent_node_id: 'absent' })])
    expect(rows.map(row => row.depth)).toEqual([1, 1, 0])
  })
})

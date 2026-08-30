import { beforeEach, describe, expect, it, vi } from 'vitest'

vi.mock('@/lib/gateway-rpc', () => ({ isMissingRestEndpoint: () => false }))
vi.mock('@/store/transcript-tail', () => ({ recordTranscriptTail: vi.fn() }))
vi.mock('@/store/connection-registry-state', () => ({
  $connectionsRegistry: {
    get: vi.fn(() => ({ connections: [] }))
  },
  hasRegistryTopology: vi.fn(() => false)
}))
vi.mock('./client', () => ({
  capabilityScoped: vi.fn(scope => (typeof scope === 'object' && scope ? { ...scope } : {})),
  getApiRequestConnection: vi.fn(() => 'prometheus'),
  hermesApi: vi.fn(),
  profileScoped: vi.fn(() => ({}))
}))

const client = await import('./client')
const registryState = await import('@/store/connection-registry-state')
const { fetchStoredTranscriptAcrossBackends, listSidebarSessions } = await import('./sessions')

const hermesApi = vi.mocked(client.hermesApi)

beforeEach(() => {
  vi.clearAllMocks()
  vi.mocked(client.getApiRequestConnection).mockReturnValue('prometheus')
})

describe('listSidebarSessions remote ownership', () => {
  it('stamps active remote rows so a later resume stays on their gateway', async () => {
    hermesApi.mockResolvedValue({
      cron: { sessions: [] },
      messaging: { sessions: [] },
      recents: {
        sessions: [{ id: 'remote-session', profile: 'default', source: 'desktop', title: 'Remote chat' }]
      }
    } as never)

    const result = await listSidebarSessions({
      recentsProfile: 'default',
      recentsLimit: 40,
      recentsExclude: [],
      cronLimit: 20,
      messagingLimit: 40,
      messagingExclude: []
    })

    expect(result.recents.sessions[0]).toMatchObject({ connection_id: 'prometheus', id: 'remote-session' })
  })
})

describe('fetchStoredTranscriptAcrossBackends', () => {
  it('probes remote connections without forcing the default profile', async () => {
    vi.mocked(registryState.$connectionsRegistry.get).mockReturnValue({
      connections: [{ id: 'prometheus' }, { id: 'remote-a' }]
    } as never)
    hermesApi
      .mockRejectedValueOnce(new Error('ambient miss'))
      .mockResolvedValueOnce({ messages: [], session_id: 'named-profile-session' } as never)

    await expect(fetchStoredTranscriptAcrossBackends('named-profile-session')).resolves.toMatchObject({
      session_id: 'named-profile-session'
    })

    expect(hermesApi).toHaveBeenLastCalledWith(expect.objectContaining({ connectionId: 'remote-a' }))
    expect(hermesApi).toHaveBeenLastCalledWith(expect.not.objectContaining({ profile: 'default' }))
  })
})

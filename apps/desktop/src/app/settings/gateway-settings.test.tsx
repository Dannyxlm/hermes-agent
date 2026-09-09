import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

// Collect the component graph before the behavioral test deadline starts.
import { GatewaySettings } from './gateway-settings'

const getConnectionConfig = vi.fn()
const saveConnectionConfig = vi.fn()
const applyConnectionConfig = vi.fn()
const oauthLoginConnectionConfig = vi.fn()
const probeConnectionConfig = vi.fn()
const notify = vi.fn()
const notifyError = vi.fn()

vi.mock('@/store/notifications', async importOriginal => ({
  ...(await importOriginal<Record<string, unknown>>()),
  notify: (...args: unknown[]) => notify(...args),
  notifyError: (...args: unknown[]) => notifyError(...args)
}))

// This test owns the machine-level GatewaySettings contract. The managed SSH
// update section mounted below the registry has its own focused coverage
// (store/managed-updates.test.ts); keep its store subscriptions out of this
// single-purpose test.
vi.mock('./managed-updates-section', () => ({ ManagedUpdatesSection: () => null }))

const localConnection = {
  cloudOrg: '',
  envOverride: false,
  mode: 'local',
  remoteAuthMode: 'token',
  remoteOauthConnected: false,
  remoteTokenPreview: null,
  remoteTokenSet: false,
  remoteUrl: ''
}

beforeEach(() => {
  applyConnectionConfig.mockReset()
  oauthLoginConnectionConfig.mockReset()
  probeConnectionConfig.mockReset()
  getConnectionConfig.mockResolvedValue(localConnection)
  saveConnectionConfig.mockResolvedValue(localConnection)
  Object.defineProperty(window, 'hermesDesktop', {
    configurable: true,
    value: {
      applyConnectionConfig,
      getConnectionConfig,
      oauthLoginConnectionConfig,
      probeConnectionConfig,
      saveConnectionConfig
    }
  })
})

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
})

describe('GatewaySettings', () => {
  it('loads the machine-level connection config (no profile scoping)', async () => {
    render(<GatewaySettings />)
    expect(await screen.findByText('Local gateway')).toBeTruthy()
    expect(
      screen.getByText('Start a private Hermes backend on localhost. This is the default and works offline.')
    ).toBeTruthy()

    // The page manages the machine's gateway connections; it must load the
    // global config, never a per-profile override.
    await waitFor(() => expect(getConnectionConfig).toHaveBeenCalledWith(null))
    expect(getConnectionConfig).not.toHaveBeenCalledWith(expect.any(String))

    // The legacy per-profile scope switcher must not render.
    expect(screen.queryByText('Applies to')).toBeNull()
    expect(screen.queryByText('All profiles')).toBeNull()
    expect(screen.queryByText('Use default gateway')).toBeNull()
  })

  const remoteConnection = {
    ...localConnection,
    mode: 'remote',
    remoteTokenSet: true,
    remoteUrl: 'https://gateway.example.com/hermes'
  }

  const signedInConnection = {
    ...remoteConnection,
    remoteAuthMode: 'oauth',
    remoteOauthConnected: true
  }

  function deferred<T>() {
    let resolve!: (value: T) => void

    const promise = new Promise<T>(settle => {
      resolve = settle
    })

    return { promise, resolve }
  }

  async function startSignIn() {
    const login = deferred<{ connected: boolean }>()
    getConnectionConfig.mockResolvedValue(remoteConnection)
    saveConnectionConfig.mockResolvedValue({ ...remoteConnection, remoteAuthMode: 'oauth' })
    probeConnectionConfig.mockResolvedValue({
      authMode: 'oauth',
      reachable: true,
      providers: [{ name: 'basic', displayName: 'Username & Password', supportsPassword: true }]
    })
    oauthLoginConnectionConfig.mockReturnValue(login.promise)
    const view = render(<GatewaySettings embedded />)
    fireEvent.click(await screen.findByRole('button', { name: 'Sign in' }))
    await waitFor(() => expect(oauthLoginConnectionConfig).toHaveBeenCalledOnce())

    return { login, view }
  }

  it('applies the authenticated gateway before reporting success, without a second save action', async () => {
    const applied = deferred<typeof signedInConnection>()
    applyConnectionConfig.mockReturnValue(applied.promise)
    const { login } = await startSignIn()

    expect(saveConnectionConfig).not.toHaveBeenCalled()
    expect(applyConnectionConfig).not.toHaveBeenCalled()
    await act(async () => login.resolve({ connected: true }))
    await waitFor(() =>
      expect(applyConnectionConfig).toHaveBeenCalledWith({
        mode: 'remote',
        remoteAuthMode: 'oauth',
        remoteUrl: remoteConnection.remoteUrl
      })
    )
    expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))

    await act(async () => applied.resolve(signedInConnection))
    expect(await screen.findByRole('button', { name: 'Sign out' })).toBeTruthy()
    expect(notify).toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
  })

  it.each(['cancelled', 'changed URL', 'closed settings', 'failed reconnect'])(
    'does not claim an activated connection after %s',
    async outcome => {
      applyConnectionConfig.mockRejectedValue(new Error('WebSocket authentication failed'))
      const { login, view } = await startSignIn()

      if (outcome === 'changed URL') {
        fireEvent.change(screen.getByPlaceholderText('https://gateway.example.com/hermes'), {
          target: { value: 'https://other.example.com' }
        })
      } else if (outcome === 'closed settings') {
        view.unmount()
      }

      await act(async () => login.resolve({ connected: outcome !== 'cancelled' }))

      if (outcome === 'failed reconnect') {
        await waitFor(() => expect(notifyError).toHaveBeenCalled())
      } else {
        expect(applyConnectionConfig).not.toHaveBeenCalled()
      }

      expect(saveConnectionConfig).not.toHaveBeenCalled()
      expect(notify).not.toHaveBeenCalledWith(expect.objectContaining({ kind: 'success' }))
    }
  )
})

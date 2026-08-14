import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it } from 'vitest'

import { AGENT_MESSAGE_RE, agentAvatarCache, AgentMessageNote } from './user-message'

afterEach(() => {
  cleanup()
  agentAvatarCache.clear()
})

// Agent-to-agent deliveries render as a compact attributed timeline notice,
// not a user bubble. This pins the detection contract: the Bot Mode prefix
// ("Message from 🤖 <sender>: …"), the handle-carrying form
// ("Message from 🤖 <sender> (@<handle>): …"), and the legacy bracket form
// all match; human prose that merely mentions the phrase does not.
describe('agent message detection', () => {
  it('matches the Bot Mode delivery prefix with sender and body', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from 🤖 Hermes: hello there')

    expect(m?.[1]?.trim()).toBe('Hermes')
    expect(m?.[4]).toBe('hello there')
  })

  it('captures the @handle when present', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from 🤖 Eats Tests (@mr-tester): run them all')

    expect(m?.[1]?.trim()).toBe('Eats Tests')
    expect(m?.[2]).toBe('mr-tester')
    expect(m?.[4]).toBe('run them all')
  })

  it('matches without the robot emoji', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from Turquoise: ready to work')

    expect(m?.[1]?.trim()).toBe('Turquoise')
    expect(m?.[4]).toBe('ready to work')
  })

  it('matches the legacy bracket form', () => {
    const m = AGENT_MESSAGE_RE.exec("[Message from agent 'turqoise'] ping")

    expect(m?.[3]).toBe('turqoise')
    expect(m?.[4]).toBe('ping')
  })

  it('spans multi-line bodies', () => {
    const m = AGENT_MESSAGE_RE.exec('Message from 🤖 Dev: line one\nline two')

    expect(m?.[4]).toBe('line one\nline two')
  })

  it('does not match prose that merely contains the phrase', () => {
    expect(AGENT_MESSAGE_RE.test('I got a Message from 🤖 Hermes: earlier')).toBe(false)
    expect(AGENT_MESSAGE_RE.test('can you explain what Message from means?')).toBe(false)
  })

  it('labels prefix-only attribution unverified and never borrows a profile avatar', () => {
    agentAvatarCache.set('hermes', 'data:image/png;base64,trusted-profile-avatar')

    const { container } = render(<AgentMessageNote text="Message from 🤖 Hermes (@hermes): hello there" />)

    expect(screen.getByText('Unverified message claiming to be from Hermes')).toBeTruthy()
    expect(container.querySelector('img')).toBeNull()
  })
})

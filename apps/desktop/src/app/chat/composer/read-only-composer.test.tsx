import { AssistantRuntimeProvider, type ThreadMessageLike, useExternalStoreRuntime } from '@assistant-ui/react'
import { act, cleanup, render } from '@testing-library/react'
import { MemoryRouter } from 'react-router'
import { afterEach, expect, it, vi } from 'vitest'

import { sessionInfo } from '@/app/session/hooks/use-prompt-actions/index-test-utils'
import { applyRuntimeInfo } from '@/app/session/hooks/use-session-actions/utils'
import { I18nProvider } from '@/i18n'
import { $readOnlyStoredTranscripts, $sessionWriteVerdicts } from '@/store/read-only-transcript'
import { $connection, $gatewayState, $sessions } from '@/store/session'

import { RICH_INPUT_SLOT } from './rich-editor'

import { ChatBar } from './index'

function MountedComposer({ session }: { session: string }) {
  const runtime = useExternalStoreRuntime({
    convertMessage: (message: ThreadMessageLike) => message,
    isRunning: false, messages: [] as ThreadMessageLike[], onNew: async () => undefined
  })

  return <AssistantRuntimeProvider runtime={runtime}><MemoryRouter><I18nProvider configClient={null} initialLocale="en">
    <ChatBar busy={false} disabled={false} gateway={null} onCancel={vi.fn()} onSubmit={vi.fn(async () => true)}
      queueSessionKey={session} sessionId={session}
      state={{ model: { canSwitch: false, model: '', provider: '' }, tools: { enabled: false, label: '' }, voice: { enabled: false, active: false } }} />
  </I18nProvider></MemoryRouter></AssistantRuntimeProvider>
}

afterEach(() => { $connection.set(null); $sessions.set([]); cleanup(); $readOnlyStoredTranscripts.set(new Set()); $sessionWriteVerdicts.set(new Map()) })

it('a successful foreign import mount disables the real composer before send; an owned/CLI verdict re-enables it', () => {
  $gatewayState.set('open')
  $sessions.set([sessionInfo({ id: 'tip', _lineage_root_id: 'import' })])
  applyRuntimeInfo({ stored_session_id: 'tip', writable: false, read_only_reason: 'session_read_only' })
  const { container } = render(<MountedComposer session="import" />)
  const editor = container.querySelector(`[data-slot="${RICH_INPUT_SLOT}"]`)
  expect(editor?.getAttribute('contenteditable')).toBe('false')
  act(() => { applyRuntimeInfo({ stored_session_id: 'tip', writable: true, read_only_reason: null }) })
  expect(editor?.getAttribute('contenteditable')).toBe('true')
})

it.each([
  { connectionId: 'A', profile: 'foreign' },
  { connectionId: 'B', profile: 'default' }
])('the compressed composer uses only its own route, not the same-id twin %j', foreign => {
  const owned = { connectionId: 'A', profile: 'default' }
  $connection.set({ ...owned, mode: 'remote' } as never)
  $gatewayState.set('open')
  $sessions.set([
    sessionInfo({ id: 'same-tip', _lineage_root_id: 'owned-root', connection_id: owned.connectionId, profile: owned.profile }),
    sessionInfo({ id: 'same-tip', _lineage_root_id: 'foreign-root', connection_id: foreign.connectionId, profile: foreign.profile })
  ])
  applyRuntimeInfo({ stored_session_id: 'same-tip', writable: false }, { owner: owned })
  const { container } = render(<MountedComposer session="owned-root" />)
  const editor = container.querySelector(`[data-slot="${RICH_INPUT_SLOT}"]`)
  expect(editor?.getAttribute('contenteditable')).toBe('false')
  act(() => { applyRuntimeInfo({ stored_session_id: 'same-tip', writable: true }, { owner: foreign, foreground: false }) })
  expect(editor?.getAttribute('contenteditable')).toBe('false')
  act(() => { applyRuntimeInfo({ stored_session_id: 'same-tip', writable: true }, { owner: owned }) })
  expect(editor?.getAttribute('contenteditable')).toBe('true')
  act(() => { applyRuntimeInfo({ stored_session_id: 'same-tip', writable: false }, { owner: foreign, foreground: false }) })
  expect(editor?.getAttribute('contenteditable')).toBe('true')
})

import {
  Badge,
  Button,
  Checkbox,
  clampHtmlNestingDepth,
  Codicon,
  ConfirmDialog,
  EmptyState,
  ErrorBoundary,
  ErrorState,
  host,
  Loader,
  RowButton,
  SearchField,
  Streamdown,
  Textarea,
  useValue
} from '@hermes/plugin-sdk'
import { useEffect, useMemo, useState } from 'react'

import { HostedRoomsController, hostedWriteIssue } from './hosted-rooms'
import type { HostedEvent, HostedRoom, HostedTask } from './hosted-rooms'
import { useHostedRoomsText } from './hosted-rooms-i18n'
import { getPluginCtx } from './shared'

function activeSource() {
  return JSON.stringify([host.state.connectionId.get() ?? 'local', host.state.profile.get() || 'default'])
}

/** Re-mount on source changes: no gateway state, drafts or requests can bleed across connections. */
export function HostedRoomsPane() {
  const connection = useValue(host.state.connectionId)
  const profile = useValue(host.state.profile)
  const source = JSON.stringify([connection ?? 'local', profile || 'default'])
  const [label, setLabel] = useState<{ id: string; text: string } | null>(null)
  useEffect(() => {
    let current = true
    void host
      .connections()
      .then(connections => {
        const selected = connections.find(row => row.id === connection)

        if (current && selected) {
          setLabel({ id: connection!, text: selected.label })
        }
      })
      .catch(() => undefined)

    return () => {
      current = false
    }
  }, [connection])

  return (
    <HostedRoomsSource
      key={source}
      source={source}
      sourceLabel={`${label?.id === connection ? label.text : (connection ?? 'local')} · ${profile || 'default'}`}
    />
  )
}

function HostedRoomsSource({ source, sourceLabel }: { source: string; sourceLabel: string }) {
  const t = useHostedRoomsText()
  const gateway = useValue(host.state.gateway)
  const visible = useValue(host.paneVisibility('hermes-bots:hosted-rooms'))

  const controller = useMemo(() => {
    const storage = getPluginCtx()?.storage

    if (!storage) {
      return null
    }

    return new HostedRoomsController({
      scope: source,
      storage,
      uuid: () => crypto.randomUUID(),
      isCurrent: () => activeSource() === source,
      request: (method, params) => {
        if (host.state.gateway.get() !== 'open') {
          return Promise.reject(new Error('unavailable'))
        }

        return host.request(method, params)
      }
    })
  }, [source])

  useEffect(() => {
    controller?.activate()

    return () => controller?.dispose()
  }, [controller])
  useEffect(() => {
    if (!controller || gateway !== 'open' || !visible) {
      return
    }

    void (controller.state.get().selected ? controller.refresh() : controller.list())

    const timer = setInterval(() => {
      if (document.visibilityState === 'hidden' || controller.state.get().hasMore) {
        return
      }

      void (controller.state.get().selected ? controller.refresh() : controller.list())
    }, 5000)

    const onFocus = () => void (controller.state.get().selected ? controller.refresh() : controller.list())
    window.addEventListener('focus', onFocus)

    return () => {
      clearInterval(timer)
      window.removeEventListener('focus', onFocus)
    }
  }, [controller, gateway, visible])

  if (!controller) {
    return <ErrorState title={t('storageUnavailable')} />
  }

  return <HostedRoomsContent controller={controller} online={gateway === 'open'} sourceLabel={sourceLabel} />
}

export function HostedRoomsContent({
  controller,
  online,
  sourceLabel
}: {
  controller: HostedRoomsController
  online: boolean
  sourceLabel: string
}) {
  const state = useValue(controller.state)
  const t = useHostedRoomsText()
  const [query, setQuery] = useState('')
  const rows = state.rooms.filter(room => `${room.name} ${room.room_id}`.toLowerCase().includes(query.toLowerCase()))
  const selected = state.selected

  return (
    <section aria-label={t('title')} className="flex h-full min-h-0 flex-col text-sm">
      <header className="flex flex-wrap items-center justify-between gap-2 p-3">
        <div className="min-w-0">
          <h2 className="font-semibold">{t('title')}</h2>
          <p className="break-all text-xs text-(--ui-text-tertiary)">
            {t('source')}: {sourceLabel}
          </p>
        </div>
        <Button
          disabled={!online || state.loading || state.writing}
          onClick={() => void controller.list()}
          size="sm"
          variant="ghost"
        >
          <Codicon name="refresh" />
          {t('refresh')}
        </Button>
        <p className="w-full text-xs text-(--ui-text-tertiary)">{t('separate')}</p>
      </header>
      <div className="flex min-h-0 flex-1 flex-wrap overflow-auto">
        <nav aria-label={t('title')} className="flex min-h-0 min-w-48 basis-56 flex-col gap-2 overflow-auto p-3">
          {state.rooms.length > 0 && (
            <SearchField aria-label={t('search')} onChange={setQuery} placeholder={t('search')} value={query} />
          )}
          {state.loading && !state.rooms.length ? <Loader label={t('title')} /> : null}
          {!state.rooms.length && !state.loading && !state.issue ? (
            <EmptyState description={t('emptyDescription')} title={t('empty')} />
          ) : null}
          {query && !rows.length ? <EmptyState title={t('noMatch')} /> : null}
          {rows.map(room => (
            <RowButton
              aria-current={selected?.room_id === room.room_id ? 'page' : undefined}
              className="flex w-full items-start gap-2 rounded-md p-2 text-left hover:bg-(--chrome-action-hover) aria-[current=page]:bg-(--chrome-action-hover)"
              disabled={!online || state.writing}
              key={room.room_id}
              onClick={() => void controller.open(room)}
            >
              <Codicon className="mt-0.5 text-(--ui-text-tertiary)" name="organization" />
              <span className="min-w-0">
                <span className="block truncate font-medium">{room.name}</span>
                <span className="block truncate text-xs text-(--ui-text-tertiary)">
                  {room.members.map(member => member.display_name || member.handle).join(', ')}
                </span>
                <span className="block break-all font-mono text-[0.625rem] text-(--ui-text-quaternary)">
                  {room.room_id}
                </span>
              </span>
            </RowButton>
          ))}
          {state.nextOffset !== null && (
            <Button
              disabled={!online || state.loading || state.writing}
              onClick={() => void controller.list(true)}
              size="sm"
              variant="ghost"
            >
              {t('moreRooms')}
            </Button>
          )}
        </nav>
        <main className="flex min-h-0 min-w-64 flex-1 flex-col p-3">
          {!online && (
            <p className="mb-3 text-xs text-(--ui-text-tertiary)" role="status">
              {t('stale')}
            </p>
          )}
          {state.issue && (
            <div className="mb-3" role="status">
              <ErrorState title={t(state.issue)} />
            </div>
          )}
          {selected ? (
            <HostedRoomDetail
              controller={controller}
              key={`${selected.room_id}:${selected.authority_gateway_id}:${selected.authority_epoch}`}
              online={online}
              room={selected}
            />
          ) : (
            <EmptyState title={t('choose')} />
          )}
        </main>
      </div>
    </section>
  )
}

function HostedRoomDetail({
  controller,
  online,
  room
}: {
  controller: HostedRoomsController
  online: boolean
  room: HostedRoom
}) {
  const state = useValue(controller.state)
  const t = useHostedRoomsText()
  const [draft, setDraft] = useState('')
  const [recipients, setRecipients] = useState(room.members.map(member => member.member_id))
  const [stopping, setStopping] = useState<HostedTask | null>(null)
  const [accepted, setAccepted] = useState(false)
  const writeIssue = hostedWriteIssue(state)
  const disabled = !online || Boolean(writeIssue) || state.writing || state.loading
  const currentMemberIds = new Set(room.members.map(member => member.member_id))
  const currentRecipients = recipients.filter(id => currentMemberIds.has(id))

  const everyoneSelected =
    room.members.length > 0 && room.members.every(member => currentRecipients.includes(member.member_id))

  useEffect(() => {
    const currentMembers = new Set(room.members.map(member => member.member_id))

    setRecipients(current => {
      const next = current.filter(id => currentMembers.has(id))

      return next.length === current.length ? current : next
    })
  }, [room.members])

  const canStop =
    online &&
    !state.writing &&
    !state.loading &&
    !hostedWriteIssue(state, 'groups.stop') &&
    state.capabilities?.features.includes('exact_task_stop') &&
    state.capabilities.methods.includes('groups.stop')

  const submit = async () => {
    const sent = await controller.send(draft, currentRecipients)

    if (sent || controller.state.get().pending) {
      setDraft('')
    }

    setAccepted(sent)
  }

  return (
    <>
      <header className="flex flex-wrap items-start justify-between gap-2 pb-3">
        <div className="min-w-0">
          <h3 className="text-base font-semibold">{room.name}</h3>
          <p className="break-all font-mono text-xs text-(--ui-text-tertiary)">{room.room_id}</p>
          <p className="break-all text-xs text-(--ui-text-tertiary)">
            {t('authority')}: {room.authority_gateway_id} · {t('epoch')} {room.authority_epoch}
          </p>
          <Badge variant="muted">
            {!online || !state.fresh
              ? t('stale')
              : state.driver?.working
                ? t('working')
                : state.hasMore
                  ? t('moreEvents')
                  : t('idle')}
          </Badge>
        </div>
        <Button
          disabled={!online || state.loading || state.writing}
          onClick={() => void controller.refresh(true)}
          size="sm"
          variant="ghost"
        >
          {t('reload')}
        </Button>
      </header>
      <ol
        aria-label={`${room.name} ${t('sequence')}`}
        className="min-h-24 flex-1 space-y-5 overflow-auto pb-4"
        role="log"
      >
        {state.events.map(event => (
          <HostedEventRow event={event} key={`${event.seq}:${event.event_id}`} room={room} />
        ))}
      </ol>
      {!state.events.length && !state.loading ? <EmptyState title={t('noEvents')} /> : null}
      {state.loading ? <Loader label={t('refresh')} /> : null}
      {state.hasMore ? (
        <Button
          disabled={!online || state.loading || state.writing}
          onClick={() => void controller.refresh()}
          size="sm"
          variant="ghost"
        >
          {t('moreEvents')}
        </Button>
      ) : null}
      {state.driver?.stoppable_tasks?.map(task => (
        <div
          className="flex flex-wrap items-center justify-between gap-2 py-2 text-xs"
          key={`${task.task_id}:${task.execution_generation}`}
        >
          <span className="break-all">
            {t('task')} {task.task_id} · {task.status}
          </span>
          <Button disabled={!canStop} onClick={() => setStopping({ ...task })} size="sm" variant="secondary">
            {t('stop')}
          </Button>
        </div>
      ))}
      {state.driver?.pending_actions?.map((action, i) => (
        <p className="py-2 text-xs text-(--ui-text-tertiary)" key={`${action.task_id}:${i}`}>
          {action.kind} · {action.task_id} — {t('pendingAction')}
        </p>
      ))}
      {writeIssue ? <p className="py-2 text-xs text-(--ui-text-tertiary)">{t(writeIssue)}</p> : null}
      {state.pending ? (
        <div className="space-y-2 py-3" role="status">
          <p className="font-medium">{t('pending')}</p>
          <p className="whitespace-pre-wrap break-words text-sm">{state.pending.payload.text}</p>
          <p className="break-all font-mono text-xs text-(--ui-text-tertiary)">{state.pending.event_id}</p>
          <Button
            disabled={
              disabled ||
              state.pending.authority.gateway_id !== room.authority_gateway_id ||
              state.pending.authority.epoch !== room.authority_epoch
            }
            onClick={() => void controller.retry().then(setAccepted)}
            size="sm"
            variant="secondary"
          >
            {t('retry')}
          </Button>
        </div>
      ) : (
        <form
          className="space-y-2 pt-3"
          onSubmit={event => {
            event.preventDefault()
            void submit()
          }}
        >
          <fieldset className="flex flex-wrap gap-x-4 gap-y-2" disabled={state.writing}>
            <legend className="mb-2 text-xs text-(--ui-text-tertiary)">{t('recipients')}</legend>
            <label className="flex items-center gap-1.5 text-xs">
              <Checkbox
                checked={everyoneSelected}
                onCheckedChange={checked => setRecipients(checked ? room.members.map(member => member.member_id) : [])}
              />
              {t('everyone')}
            </label>
            {room.members.map(member => (
              <label className="flex items-center gap-1.5 text-xs" key={member.member_id}>
                <Checkbox
                  checked={currentRecipients.includes(member.member_id)}
                  onCheckedChange={checked =>
                    setRecipients(current =>
                      checked
                        ? [...current.filter(id => id !== member.member_id), member.member_id]
                        : current.filter(id => id !== member.member_id)
                    )
                  }
                />
                {member.display_name || member.handle}
              </label>
            ))}
          </fieldset>
          <Textarea
            aria-label={t('message')}
            disabled={state.writing}
            onChange={event => {
              setDraft(event.target.value)
              setAccepted(false)
            }}
            placeholder={t('message')}
            value={draft}
          />
          <div className="flex items-center justify-between gap-2">
            <span className="text-xs text-(--ui-text-tertiary)" role="status">
              {accepted ? t('accepted') : ''}
            </span>
            <Button disabled={disabled || !draft.trim() || !currentRecipients.length} size="sm" type="submit">
              {t('send')}
            </Button>
          </div>
        </form>
      )}
      <ConfirmDialog
        confirmLabel={t('stop')}
        description={
          <>
            <p>{t('stopDescription')}</p>
            <p className="break-all font-mono text-xs">
              {stopping?.task_id} · {stopping?.execution_generation}
            </p>
          </>
        }
        doneLabel={t('stopRequested')}
        onClose={() => setStopping(null)}
        onConfirm={async () => {
          if (stopping && !(await controller.stop(stopping))) {
            throw new Error(t('staleTask'))
          }
        }}
        open={Boolean(stopping)}
        title={t('stopTitle')}
      />
    </>
  )
}

function HostedEventRow({ event, room }: { event: HostedEvent; room: HostedRoom }) {
  const t = useHostedRoomsText()
  const member = room.members.find(candidate => candidate.member_id === event.actor.id)
  const text = typeof event.payload.text === 'string' ? event.payload.text : null

  return (
    <li className="min-w-0">
      <div className="mb-1 flex flex-wrap items-center gap-2 text-xs text-(--ui-text-tertiary)">
        <span className="font-medium text-(--ui-text-secondary)">
          {member?.display_name || member?.handle || event.actor.id}
        </span>
        <span>{event.kind}</span>
        <span>
          {t('sequence')} {event.seq} · {t('epoch')} {event.authority_epoch}
        </span>
        <time dateTime={new Date(event.created_at * 1000).toISOString()}>
          {new Date(event.created_at * 1000).toLocaleString()}
        </time>
      </div>
      {text ? (
        <HostedEventText text={text} />
      ) : (
        <p className="break-all font-mono text-xs text-(--ui-text-tertiary)">{event.event_id}</p>
      )}
    </li>
  )
}

function HostedEventText({ text }: { text: string }) {
  const renderableText = useMemo(() => clampHtmlNestingDepth(text), [text])

  return (
    <ErrorBoundary
      fallback={() => <p className="whitespace-pre-wrap break-words">{text}</p>}
      label="hosted-room-markdown"
    >
      <div className="break-words">
        <Streamdown>{renderableText}</Streamdown>
      </div>
    </ErrorBoundary>
  )
}

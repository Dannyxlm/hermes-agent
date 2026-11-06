import { useState } from 'react'

import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { EmptyState } from '@/components/ui/empty-state'
import { ErrorState } from '@/components/ui/error-state'
import { Loader } from '@/components/ui/loader'
import { useI18n } from '@/i18n'

import { familyChatTarget, type FamilyTarget, openFamilyChat, targetForProfile } from './actions'
import { type FamilyDisplayNode, familyRows } from './model'
import { useFamily } from './use-family'

interface FamilyDialogProps {
  target: FamilyTarget | null
  onClose: () => void
}

export function FamilyDialog({ target, onClose }: FamilyDialogProps) {
  const { t } = useI18n()

  return (
    <Dialog onOpenChange={open => { if (!open) { onClose() } }} open>
      <DialogContent bodyClassName="gap-4" className="max-w-2xl">
        <DialogHeader>
          <DialogTitle>{t.family.title}</DialogTitle>
          <DialogDescription>{t.family.description}</DialogDescription>
        </DialogHeader>
        {target ? <FamilyBrowser initialTarget={target} key={JSON.stringify(target)} onOpened={onClose} />
          : <EmptyState title={t.family.noOwner} />}
      </DialogContent>
    </Dialog>
  )
}

function FamilyBrowser({ initialTarget, onOpened }: { initialTarget: FamilyTarget; onOpened: () => void }) {
  const { t } = useI18n()
  const c = t.family
  const [target, setTarget] = useState(initialTarget)
  const [overview, setOverview] = useState(false)
  const [revision, setRevision] = useState(0)
  const read = useFamily(target, overview, revision)
  const [opening, setOpening] = useState<string | null>(null)
  const [openError, setOpenError] = useState<string | null>(null)

  const openChat = async (node: FamilyDisplayNode, answer: boolean) => {
    if (opening) { return }
    const destination = familyChatTarget(target, node, answer)

    if (!destination) { return }
    setOpening(node.node_id)
    setOpenError(null)

    try {
      await openFamilyChat(destination)
      onOpened()
    } catch (error) {
      setOpenError(error instanceof Error ? error.message : String(error))
    } finally { setOpening(null) }
  }

  return (
    <>
      <div className="flex items-center gap-2">
        <Button onClick={() => {
          setTarget(initialTarget); setOverview(false)
        }} size="sm" variant={!overview ? 'secondary' : 'ghost'}>{c.thisChat}</Button>
        <Button onClick={() => setOverview(true)} size="sm" variant={overview ? 'secondary' : 'ghost'}>{c.overview}</Button>
        <div className="flex-1" />
        <Button onClick={() => setRevision(value => value + 1)} size="sm" variant="ghost">{c.refresh}</Button>
      </div>
      {openError && <div className="text-sm text-destructive whitespace-pre-wrap" role="alert">{openError}</div>}
      {read.kind === 'loading' && <Loader label={c.title} />}
      {read.kind === 'unavailable' && <EmptyState title={c.unavailable} />}
      {read.kind === 'error' && <ErrorState description={read.message} title={c.failed} />}
      {read.kind === 'ready' && <>
        {(!read.view.complete || read.view.unavailable_profiles.length > 0) &&
          <p className="text-sm text-(--ui-text-secondary)" role="status">{c.partial} {read.view.unavailable_profiles.join(', ')}</p>}
        {read.view.truncated && <p className="text-sm text-(--ui-text-secondary)" role="status">{c.truncated}</p>}
        {!overview && read.view.root && <div className="text-sm font-medium break-words">
          {read.view.root.full_title || read.view.root.title || c.untitled}
        </div>}
        {read.view.nodes.length === 0 ? <EmptyState title={c.empty} /> :
          <ul aria-label={c.title} className="grid gap-1">
            {familyRows(read.view.nodes).map(({ node, depth }) => (
              <li className="flex flex-wrap items-start gap-3 py-3" key={`${node.profile}:${node.node_id}`} style={{ paddingInlineStart: `${depth * 0.75}rem` }}>
                <Codicon className="mt-1 text-(--ui-text-tertiary)" name="git-branch" />
                <div className="min-w-0 flex-1 basis-48">
                  <div className="break-words text-sm font-medium">{node.full_title || node.title || c.untitled}</div>
                  <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-(--ui-text-secondary)">
                    <span>{node.profile}</span>
                    <Badge size="xs" variant={node.status === 'failed' ? 'destructive' : node.status === 'needs_you' ? 'warn' : 'muted'}>{c.status[node.status]}</Badge>
                  </div>
                  {node.activity_text && <p className="mt-1 break-words text-xs text-(--ui-text-secondary)">{node.activity_text}</p>}
                </div>
                <div className="flex flex-wrap gap-1">
                  {overview && node.session_id && <Button disabled={Boolean(opening)} onClick={() => {
                    setTarget(targetForProfile(target, node.profile, node.session_id ?? '')); setOverview(false)
                  }} size="xs" variant="ghost">{c.browse}</Button>}
                  {familyChatTarget(target, node, true) && <Button disabled={Boolean(opening)} onClick={() => { void openChat(node, true) }} size="xs" variant="secondary">{c.answer}</Button>}
                  {familyChatTarget(target, node, false) && <Button disabled={Boolean(opening)} onClick={() => { void openChat(node, false) }} size="xs" variant="ghost">{opening === node.node_id ? c.opening : c.open}</Button>}
                  {!familyChatTarget(target, node, false) && !familyChatTarget(target, node, true) && !overview && <span className="text-xs text-(--ui-text-tertiary)">{c.readOnly}</span>}
                </div>
              </li>
            ))}
          </ul>}
      </>}
    </>
  )
}

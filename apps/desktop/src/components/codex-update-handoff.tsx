import { useState } from 'react'

import type { DesktopUpdateStatus } from '@/global'
import { useI18n } from '@/i18n'
import { codexUpdateRequest } from '@/lib/codex-update'

import { CopyButton } from './ui/copy-button'

export function CodexUpdateHandoff({ status }: { status: DesktopUpdateStatus }) {
  const { t } = useI18n()
  const u = t.updates
  const request = codexUpdateRequest(status)
  const [copiedRequest, setCopiedRequest] = useState<string | null>(null)

  return (
    <div className="grid gap-2">
      <CopyButton
        buttonVariant="default"
        label={u.codexUpdateAction}
        onCopied={() => setCopiedRequest(request)}
        onCopyError={() => setCopiedRequest(null)}
        text={request}
      />
      <p aria-live="polite" className="text-center text-xs text-muted-foreground">
        {copiedRequest === request ? u.codexUpdateCopied : u.codexUpdateHint}
      </p>
    </div>
  )
}

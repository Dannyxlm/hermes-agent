import { useState } from 'react'

import { Button } from '@/components/ui/button'
import { Codicon } from '@/components/ui/codicon'
import { useI18n } from '@/i18n'

import { familyTarget, type FamilyTarget } from './actions'
import { FamilyDialog } from './dialog'

/** Visible door for the selected chat; the same browser also serves tab and row menus. */
export function FamilyButton({ sessionId }: { sessionId: string }) {
  const { t } = useI18n()
  const [family, setFamily] = useState<{ target: FamilyTarget | null } | null>(null)

  return <>
    <Button className="pointer-events-auto [-webkit-app-region:no-drag]" disabled={!sessionId} onClick={() => setFamily({ target: familyTarget(sessionId) })} size="sm" variant="ghost">
      <Codicon name="git-branch" />{t.family.title}
    </Button>
    {family && <FamilyDialog onClose={() => setFamily(null)} target={family.target} />}
  </>
}

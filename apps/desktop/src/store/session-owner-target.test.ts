import { afterEach, expect, it } from 'vitest'

import { _resetSessionOwnerHintsForTests, getSessionOwnerHint, getSessionOwnerHints, hydrateSessionOwnerHints, setSessionOwnerHint } from './session'

afterEach(() => _resetSessionOwnerHintsForTests({ storage: true }))
it('persists backend-target identity independently of the Desktop dial alias and fails closed for partial or unqualified ambiguity', () => {
  const first = { connectionId: 'box', profile: 'alias', targetProfile: 'default' }
  const second = { ...first, targetProfile: 'reviewer' }
  setSessionOwnerHint('same-id', first)
  setSessionOwnerHint('same-id', second)
  expect(getSessionOwnerHints('same-id')).toEqual([first, second])

  for (const scope of [undefined, { connectionId: 'box' }, { connectionId: 'box', profile: 'alias' }]) {
    expect(getSessionOwnerHint('same-id', scope)).toBeUndefined()
  }

  expect(getSessionOwnerHint('same-id', first)).toEqual(first)
  expect(getSessionOwnerHint('same-id', second)).toEqual(second)
  _resetSessionOwnerHintsForTests()
  hydrateSessionOwnerHints()
  expect(getSessionOwnerHints('same-id')).toEqual([first, second])
  expect(getSessionOwnerHint('same-id', second)).toEqual(second)
})
it('normalizes a legacy implicit target to the dial profile without duplicating it, retaining bounded LRU order', () => {
  setSessionOwnerHint('legacy', { connectionId: ' box ', profile: ' alias ' })
  setSessionOwnerHint('legacy', { connectionId: 'box', profile: 'alias', targetProfile: 'alias' })
  expect(getSessionOwnerHints('legacy')).toHaveLength(1)
  expect(getSessionOwnerHint('legacy', { connectionId: 'box', profile: 'alias' })).toMatchObject({ targetProfile: 'alias' })

  for (let index = 0; index < 256; index++) {
    setSessionOwnerHint(`filler-${index}`, { connectionId: 'box', profile: 'alias', targetProfile: 'reviewer' })
  }

  expect(getSessionOwnerHint('legacy')).toBeUndefined()
  expect(getSessionOwnerHint('filler-255')).toMatchObject({ targetProfile: 'reviewer' })
})

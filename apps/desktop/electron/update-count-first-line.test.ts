import { describe, expect, it } from 'vitest'
import { firstLine } from './update-count'

describe('shared isolated-repository diagnostic line', () => {
  it('uses the same first nonempty line as the previous supervisor helper', () => {
    expect(firstLine('\n\ninitialization failed\ndetail')).toBe('initialization failed')
    expect(firstLine('')).toBe('')
    expect(firstLine('   \ndetail')).toBe('   ')
  })
})

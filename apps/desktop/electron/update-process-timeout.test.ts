import assert from 'node:assert/strict'

import { test, vi } from 'vitest'

import { terminateTimedOutProcess } from './update-process-timeout'

test('Windows timeout falls back to the direct child when taskkill fails', () => {
  const kill = vi.fn()
  const forceKillProcessTree = vi.fn(() => false)

  terminateTimedOutProcess(
    { kill, pid: 42 },
    {
      forceKillProcessTree,
      isWindows: true
    }
  )

  assert.deepEqual(forceKillProcessTree.mock.calls, [[42]])
  assert.deepEqual(kill.mock.calls, [['SIGKILL']])
})

test('Windows timeout does not double-kill after taskkill succeeds', () => {
  const kill = vi.fn()

  terminateTimedOutProcess(
    { kill, pid: 42 },
    {
      forceKillProcessTree: () => true,
      isWindows: true
    }
  )

  assert.equal(kill.mock.calls.length, 0)
})

test('POSIX timeout kills the detached process group and falls back on failure', () => {
  const groupKill = vi.fn(() => {
    throw new Error('missing group')
  })

  const kill = vi.fn()

  terminateTimedOutProcess(
    { kill, pid: 42 },
    {
      forceKillProcessTree: () => false,
      isWindows: false,
      killProcessGroup: groupKill
    }
  )

  assert.deepEqual(groupKill.mock.calls, [[-42, 'SIGKILL']])
  assert.deepEqual(kill.mock.calls, [['SIGKILL']])
})

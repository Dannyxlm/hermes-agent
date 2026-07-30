interface TimedOutProcess {
  kill: (signal: 'SIGKILL') => unknown
  pid?: number
}

interface TerminateTimedOutProcessOptions {
  forceKillProcessTree: (pid: number) => boolean
  isWindows: boolean
  killProcessGroup?: (pid: number, signal: 'SIGKILL') => unknown
}

function terminateTimedOutProcess(
  child: TimedOutProcess,
  {
    forceKillProcessTree,
    isWindows,
    killProcessGroup = (pid, signal) => process.kill(pid, signal)
  }: TerminateTimedOutProcessOptions
): void {
  const pid = Number.isInteger(child.pid) ? (child.pid as number) : null

  if (isWindows && pid !== null) {
    if (!forceKillProcessTree(pid)) {
      child.kill('SIGKILL')
    }

    return
  }

  if (pid !== null) {
    try {
      killProcessGroup(-pid, 'SIGKILL')

      return
    } catch {
      // The process may have exited or may not be a group leader. Fall back to
      // the direct child so the timeout promise still reaches an exit event.
    }
  }

  child.kill('SIGKILL')
}

export { terminateTimedOutProcess }

"""Fail-open accident prevention for ordinary local Bash ``kill`` commands.

Not a security boundary: explicit binaries, other interpreters and nested shells
can bypass this. Never change receiver signal handlers or service-manager stops.
"""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

logger = logging.getLogger(__name__)

# The word, not the substring: `skills` paths and `pkill` must not switch the guard on.
_KILL_WORD = re.compile(r"(?<![A-Za-z0-9_])kill(?![A-Za-z0-9_])")
# Linux refuses a single argument of 128 KiB (MAX_ARG_STRLEN); never push a command over it.
_MAX_ARGUMENT_BYTES = 131072


def _protected_targets() -> set[int]:
    """Interpreter/ancestor PIDs and negative PGIDs, captured before spawning a shell."""
    import psutil

    current = psutil.Process()
    processes = [current, *current.parents()]
    targets = {-1}  # Broadcast includes the interpreter, not just the tool's children.
    for process in processes:
        if process.pid > 1:
            targets.add(process.pid)
            try:
                targets.add(-os.getpgid(process.pid))
            except OSError:
                pass  # Keep the PIDs already found: one vanished ancestor must not switch the guard off.
    return targets


_KILL_FUNCTION = r'''
function kill {
    local __h_signal=TERM __h_target __h_number
    local -a __h_original=("$@")
    case "${1-}" in
        -l|-L) builtin kill "$@"; return ;;
        -s|-n) __h_signal="${2-}"; shift 2 || { builtin kill "${__h_original[@]}"; return; } ;;
        --) shift ;;
        -*) __h_signal="${1#-}"; shift ;;
    esac
    if [[ "$__h_signal" =~ ^0+$ ]]; then
        # Bash lets a later -s/-n override the zero, so only a call without one is a pure probe.
        local __h_later=
        for __h_target in "$@"; do [[ "$__h_target" == -[sn]* ]] && __h_later=1; done
        [[ -n "$__h_later" ]] || { builtin kill "${__h_original[@]}"; return; }
    fi
    for __h_target in "$@"; do
        # Bash's kill accepts padding and a plus sign, and `ps -o ppid=` pads its output.
        __h_target="${__h_target#"${__h_target%%[![:space:]]*}"}"
        __h_target="${__h_target%"${__h_target##*[![:space:]]}"}"
        __h_target="${__h_target#+}"
        [[ "$__h_target" =~ ^-?[0-9]+$ ]] || continue
        __h_number="${__h_target#-}"
        while [[ ${#__h_number} -gt 1 && "$__h_number" == 0* ]]; do
            __h_number="${__h_number#0}"
        done
        [[ "$__h_target" == -* ]] && __h_number="-$__h_number"
        case "$__h_number" in
            __TARGETS__)
                printf 'host-stop guard: refused signal=%s target=%s (tool host or ancestor); stop owned children only, or use an authorized external service-control shell.\n' "$__h_signal" "$__h_target" >&2
                return 1 ;;
        esac
    done
    builtin kill "${__h_original[@]}"
}
'''


def guard_shell_command(command: str, *, shell: str) -> str:
    """Install a non-exported function; inspection failure leaves the command untouched.

    Resolve arguments in the shell, not with a source-string scanner: ``kill "$pid"``
    is exactly the accidental computed-parent case. No receiver-side ambiguity can
    turn a legitimate external stop into a refusal.
    """
    if not _KILL_WORD.search(command) or os.name != "posix" or Path(shell).name != "bash":
        return command
    try:
        targets = _protected_targets()
    except Exception as exc:
        logger.debug("Host-stop guard unavailable: %s", type(exc).__name__, exc_info=True)
        return command
    pattern = "|".join(str(target) for target in sorted(targets))
    guarded = _KILL_FUNCTION.replace("__TARGETS__", pattern) + "\n" + command
    if len(guarded.encode("utf-8", "surrogateescape")) >= _MAX_ARGUMENT_BYTES:
        return command
    return guarded

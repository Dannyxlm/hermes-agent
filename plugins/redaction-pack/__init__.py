"""redaction-pack: teach Hermes's secret redactor more vendor credential formats.

``register()`` hands the patterns in ``patterns.py`` to ``ctx.register_redaction_patterns``.
Hermes adds them to the prefix matcher it already runs over logs, tool output and
model-bound text; the seam is additive-only, so the pack can widen what gets masked
but cannot weaken a built-in rule, and ``security.redact_secrets: false`` still turns
redaction off as a whole. Nothing else is registered: no tools, hooks, network,
files or subprocesses.
"""

from __future__ import annotations

import logging

from .patterns import PATTERNS

logger = logging.getLogger(__name__)


def register(ctx) -> None:
    accepted = ctx.register_redaction_patterns(list(PATTERNS))
    # Fewer than len(PATTERNS) is expected once Hermes ships one of these formats as a
    # built-in (duplicates are skipped); Hermes logs a warning for any it refused.
    logger.debug("redaction-pack: %s of %d credential formats registered", accepted, len(PATTERNS))

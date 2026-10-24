"""Duty Board: dashboard, model-free chat summary and local CLI."""
import logging

__version__ = "0.4.0"
_DESCRIPTION = "Duty Board summary: what is waiting on you, running, failing"
logger = logging.getLogger(__name__)


def register(ctx) -> None:
    # Each surface is optional on older hosts. Import only stdlib-backed
    # command modules here; FastAPI belongs exclusively to the dashboard.
    try:
        from .dashboard.commands import duty
        ctx.register_command("duty", duty, description=_DESCRIPTION, args_hint="[full|counts] [lang]")
    except Exception:
        logger.exception("Duty Board slash registration unavailable")
    try:
        from .dashboard.commands import cli_handler, setup_cli
        ctx.register_cli_command("duty", help=_DESCRIPTION, setup_fn=setup_cli, handler_fn=cli_handler, description=_DESCRIPTION)
    except Exception:
        logger.exception("Duty Board CLI registration unavailable")

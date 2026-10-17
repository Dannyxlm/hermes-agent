"""Creation-time model policy and explicit shared-storage intent."""
import os
from pathlib import Path


def creation_model(conn, model, provider, board=None):
    """Freeze board defaults on the row, never re-resolve them at dispatch."""
    from hermes_cli import kanban_db as kb
    from hermes_cli.config_effective import load_user_config_effective

    from hermes_cli.kanban_db_connect import _main_db_file

    config = (load_user_config_effective(fail_closed=True) or {}).get("kanban", {})

    slug = board or kb.get_current_board()
    db_file = _main_db_file(conn)
    if board is None and db_file:
        parent = Path(db_file).parent
        if parent.parent.resolve() == kb.boards_root().resolve():
            slug = parent.name
        elif Path(db_file).resolve() == kb.kanban_db_path(kb.DEFAULT_BOARD).resolve():
            slug = kb.DEFAULT_BOARD
    defaults = config.get("board_models", {}).get(slug, {})
    model = (model or "").strip() or None
    provider = (provider or "").strip() or None
    # Never pair an explicit card model with a different board model's provider.
    if model is None:
        model = (defaults.get("model") or "").strip() or None
        provider = provider or (defaults.get("provider") or "").strip() or None
    required = config.get("require_model")
    if required is None:
        required = kb.read_board_metadata(slug).get("require_model", False)
    if not isinstance(required, bool):
        raise ValueError("kanban.require_model must be true, false or null")
    if model is None and required:
        raise ValueError(
            "no model for this card: pass --model/--provider or set a board default with "
            f"hermes config set kanban.board_models.{slug}.model MODEL "
            f"and hermes config set kanban.board_models.{slug}.provider PROVIDER"
        )
    return kb._validate_model_override(model, provider)


def prepare_board(board=None):
    """Surface bootstrap: a new default board is strict, existing DBs stay compatible."""
    from hermes_cli import kanban_db as kb

    slug = board or kb.get_current_board()
    path = kb.kanban_db_path(board)
    assert_storage_intent(path)
    if slug == kb.DEFAULT_BOARD and not path.exists():
        kb.write_board_metadata(slug)


def assert_storage_intent(path: Path):
    """Custom homes can share intentionally; an accidental native-root fallthrough cannot."""
    from hermes_constants import _get_platform_default_hermes_home, get_hermes_home

    if os.environ.get("HERMES_KANBAN_HOME", "").strip():
        return
    native = _get_platform_default_hermes_home().resolve()
    home = get_hermes_home().resolve()
    target = Path(path).resolve()
    if home == native or not target.is_relative_to(native):
        return
    # -p/--profile and multiplex scopes intentionally join the shared board.
    if home.parent == native / "profiles" and not home.name.startswith("."):
        return
    raise PermissionError(
        "isolated HERMES_HOME would write the shared Kanban board: "
        "set HERMES_KANBAN_HOME to isolate Kanban "
        "(or explicitly set it to the shared root to opt in)"
    )


def set_model_override(conn, task_id, model, provider=None):
    """Refuse late pins atomically with the dispatch claim, not after it."""
    from hermes_cli import kanban_db as kb

    model, provider = kb._validate_model_override(model, provider)
    with kb.write_txn(conn):
        status = kb._task_status(conn, task_id)
        if status is None:
            return False
        if status == "running":
            raise RuntimeError(
                "cannot pin a running card: the running worker keeps its model; "
                "wait for completion or stop/reclaim the worker before setting the next model"
            )
        if status == "archived":
            raise RuntimeError(f"cannot set model override on archived task {task_id}")
        conn.execute("UPDATE tasks SET model_override = ?, provider_override = ? WHERE id = ?",
                     (model, provider, task_id))
        kb._append_event(conn, task_id, "model_override_set", {"model": model, "provider": provider})
    kb.notify_task_updated(conn, task_id, ("model_override", "provider_override"))
    return True

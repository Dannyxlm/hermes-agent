"""Skin + config-change watcher: on-disk signatures for skin/pet/cron/sessions/platforms/
pairing/bot-relay state and the broadcast loop that pushes *.changed events. Bodies are
rebound onto server.py's globals at install time (method_ctx.bind_module)."""

from __future__ import annotations

from pathlib import Path

from .method_ctx import HandlerRegistry, bind_module

_registry = HandlerRegistry()


def resolve_skin() -> dict:
    try:
        from hermes_cli.skin_engine import init_skin_from_config, get_active_skin
        init_skin_from_config(_load_cfg())
        skin = get_active_skin()
        # light/dark are paired palettes: the TUI prefers the block matching terminal polarity.
        return {
            "name": skin.name, "colors": skin.colors,
            "light_colors": skin.light_colors, "dark_colors": skin.dark_colors,
            "branding": skin.branding, "banner_logo": skin.banner_logo,
            "banner_hero": skin.banner_hero, "tool_prefix": skin.tool_prefix,
            "help_header": (skin.branding or {}).get("help_header", ""),
            # Raw user CSS for the desktop GUI's <style> tag (32 KiB cap in the skin engine).
            "customCSS": skin.custom_css}
    except Exception:
        return {}


# (name, user-file mtime) of the last skin broadcast: ``skin.changed`` fires on a name
# switch OR a live color edit of the active skin, and nothing else.
_last_skin_sig: tuple[str, float | None] | None = None


def _watcher_home() -> Path:
    """Active profile home for the change watcher's signature probes."""
    override = get_hermes_home_override()
    return Path(override if isinstance(override, str) and override else _hermes_home)


def _watcher_mtime_ns(path: Path):
    """``st_mtime_ns`` of ``path``, or None when it cannot be stat'ed."""
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return None


def _watcher_size(path: Path):
    """``st_size`` of ``path``, or None when it cannot be stat'ed."""
    try:
        return path.stat().st_size
    except OSError:
        return None


def _home_mtime_ns(*parts: str):
    return _watcher_mtime_ns(_watcher_home().joinpath(*parts))


def _newest_mtime_ns(paths) -> int | None:
    """Max ``st_mtime_ns`` across ``paths`` (unstat-able ignored); None when none stat'ed."""
    return max((m for m in map(_watcher_mtime_ns, paths) if m is not None), default=None)


def _skin_sig() -> tuple[str, float | None]:
    """(active skin name, its user-file mtime). Built-ins have no file, so only
    their name moves; a user skin's mtime lets an in-place color edit repaint too."""
    name = str((_load_cfg().get("display") or {}).get("skin") or "default")
    try:
        return name, (_watcher_home() / "skins" / f"{name}.yaml").stat().st_mtime
    except OSError:
        return name, None


def _note_skin_broadcast() -> None:
    """Sync the baseline after the /skin RPC emits so the watcher doesn't re-broadcast it."""
    global _last_skin_sig
    with contextlib.suppress(Exception):
        _last_skin_sig = _skin_sig()


def _broadcast_skin_if_changed() -> None:
    """Emit ``skin.changed`` when the active skin moved, via the SAME live path as
    ``/skin`` so every surface repaints. The check is a dict lookup + one stat."""
    global _last_skin_sig
    with contextlib.suppress(Exception):
        sig = _skin_sig()
        if sig == _last_skin_sig:
            return
        _last_skin_sig = sig
        _broadcast_global_event("skin.changed", resolve_skin())


def _active_pet():
    """(pet, scale) when an enabled pet with an existing sheet is selected, else None."""
    enabled, pet, scale = _pet_active_selection()
    return (pet, scale) if enabled and pet is not None and pet.exists else None


def _pet_sig() -> tuple:
    """(slug, spritesheet revision, scale) of the active pet — ("off",) when none."""
    display = _load_cfg().get("display") or {}
    pet_cfg = display.get("pet") if isinstance(display.get("pet"), dict) else {}
    if not pet_cfg or not is_truthy_value(pet_cfg.get("enabled"), default=False):
        return ("off",)
    try:
        if active := _active_pet():
            pet, scale = active
            return (pet.slug, _pet_sheet_revision(pet.spritesheet), scale)
    except Exception:  # noqa: BLE001 - cosmetic, never break the watcher
        pass
    return ("off",)


def _pet_changed_payload() -> dict:
    """``pet.info.meta``-shaped payload so the renderer can decide whether to refetch sprites."""
    try:
        if active := _active_pet():
            pet, scale = active
            return {"enabled": True, "slug": pet.slug, "displayName": pet.display_name,
                    "scale": scale, "spritesheetRevision": _pet_sheet_revision(pet.spritesheet)}
    except Exception:  # noqa: BLE001 - cosmetic, never break the watcher
        pass
    return {"enabled": False}


# last_activity_at / _description are left out on purpose: the session activity heartbeat restamps
# them mid-turn and on idle ticks, so hashing them re-fired sessions.changed every heartbeat window
# (#98005). Liveness comes from session.active_list, and a real turn still moves message_count.
_SESSION_SIGNATURE_FIELDS = (
    "id", "source", "session_key", "display_name", "model", "parent_session_id",
    "started_at", "ended_at", "end_reason", "message_count", "tool_call_count",
    "cwd", "git_branch", "git_repo_root", "title", "title_source", "profile_name",
    "archived", "pinned", "hidden", "last_read_at", "handoff_state",
)
# path -> ((database/WAL mtime, db size, WAL size), session-table digest). The stat guard keeps the
# normal 0.5 s watch pass stat-only; SQLite is read only after another process
# actually commits.
_sessions_db_sig_cache: dict[str, tuple[tuple, tuple | None]] = {}


def _session_db_content_sig(db_path: Path):
    """Digest list/transcript-relevant session rows, excluding unrelated tables.

    ``gateway_heartbeats`` shares state.db and writes every minute. Using the
    database mtime directly therefore emits sessions.changed while no session
    changed (#98005). Cache behind the DB/WAL mtime, then inspect only the
    sessions columns that drive Desktop projections. Legacy stores safely use
    the subset of columns they have.
    """
    mtime = _newest_mtime_ns((db_path, db_path.with_name(f"{db_path.name}-wal")))
    # Sizes join the guard: mtimes are jiffy-coarse, so two commits a few ms apart can share one,
    # and the cursor journal must not miss the second. A WAL append always grows the file.
    stamp = (mtime, *(_watcher_size(p) for p in (db_path, db_path.with_name(f"{db_path.name}-wal"))))
    # One entry per real file: the watcher, list reads and the push worker spell homes differently.
    cache_key = os.path.realpath(db_path)
    cached = _sessions_db_sig_cache.get(cache_key)

    if cached is not None and cached[0] == stamp:
        return cached[1]
    if not db_path.exists():
        _sessions_db_sig_cache[cache_key] = (stamp, None)

        return None

    conn = None
    try:
        import hashlib
        from hermes_state import _connect_tracked_db
        from hermes_state_holders import read_only_db_uri

        conn = _connect_tracked_db(read_only_db_uri(db_path), tracking_path=db_path,
                                   uri=True, timeout=0.05)
        available = {row[1] for row in conn.execute("PRAGMA table_info(sessions)")}
        fields = tuple(field for field in _SESSION_SIGNATURE_FIELDS if field in available)
        if not fields:
            # A state.db without a readable sessions table (foreign schema, legacy
            # or transient file): keep the old mtime contract so any move still
            # wakes the sidebar instead of silently never broadcasting.
            signature = ("mtime-fallback", mtime)
        else:
            order = " ORDER BY id" if "id" in available else ""
            rows = conn.execute(f"SELECT {', '.join(fields)} FROM sessions{order}")
            digest = hashlib.blake2b(digest_size=16)
            # The same read feeds the per-scope change journal behind the sessions.changed cursor.
            observed, id_at = {}, fields.index("id") if "id" in fields else None
            for row in rows:
                values = tuple(row)
                encoded = repr(values).encode("utf-8", "backslashreplace")
                digest.update(encoded)
                digest.update(b"\0")
                if id_at is not None:
                    observed[values[id_at]] = (encoded, values)
            signature = (fields, digest.digest())
            if id_at is not None:
                from tui_gateway import session_change_cursor
                try:
                    session_change_cursor.observe(
                        db_path, fields, observed,
                        delegate_ids=lambda ids: _delegate_session_ids(conn, ids, available))
                except Exception:  # noqa: BLE001 - the journal must never change the legacy signal
                    logger.debug("session change journal update failed", exc_info=True)
    except Exception:  # noqa: BLE001 - preserve the old wake-up signal if the read probe cannot run
        # A busy/locked read after a good one keeps the last digest and leaves the cached mtime
        # stale so the next pass re-reads: digest -> mtime -> digest would broadcast twice.
        if cached is not None and cached[1] is not None and cached[1][0] != "mtime-fallback":
            return cached[1]
        signature = ("mtime-fallback", mtime)
    finally:
        if conn is not None:
            conn.close()

    _sessions_db_sig_cache[cache_key] = (stamp, signature)

    return signature


def _delegate_session_ids(conn, ids: list, available: set) -> set:
    """Ids among ``ids`` that are delegated child runs (``_delegate_from`` marker): many inherit
    their parent's surface as ``source``, so only the marker tells them from a conversation."""
    if "model_config" not in available:
        return set()
    from hermes_state_common import _id_chunks, _placeholders
    from hermes_state_sessions import _delegate_from_json
    found = set()
    for chunk in _id_chunks(ids):
        found.update(row[0] for row in conn.execute(
            f"SELECT id FROM sessions WHERE id IN ({_placeholders(chunk)}) "
            f"AND {_delegate_from_json()} IS NOT NULL", chunk))
    return found


def _sessions_sig():
    """Session-table content across the active and served profile stores.

    Messaging-gateway turns and cron runs are written by other processes that
    never touch this gateway's transports, so their session rows are the shared
    change signal. Hashing only those rows avoids false Desktop refreshes from
    unrelated state.db writes such as gateway heartbeats.
    """
    return tuple(
        _session_db_content_sig(root / "state.db")
        for root in (_watcher_home(), *_served_profile_homes)
    )


def _projects_sig():
    """Newest mtime across projects.db (+ WAL) for the watcher home and every served
    sibling profile. The CLI and other windows write projects.db directly — nothing in
    their process touches this gateway's transports — so the file is the only shared
    signal, exactly like state.db (#53046, #56757)."""
    return _newest_mtime_ns(
        root / name
        for root in (_watcher_home(), *_served_profile_homes)
        for name in ("projects.db", "projects.db-wal"))


def _pairing_sig():
    """Newest mtime across every profile's pairing ledgers (legacy ``pairing/`` and
    ``platforms/pairing/``): the gateway process writes pending codes, so the files are the only
    shared signal (a pairing request moves nothing in gateway_state.json)."""
    entries = []
    for root in _pairing_roots(_watcher_home()):
        with contextlib.suppress(OSError):
            # Only the ledgers: _rate_limits.json moves on every unauthorized DM.
            entries += [
                e for e in root.iterdir() if e.name.endswith(("-pending.json", "-approved.json"))]
    return _newest_mtime_ns(entries)


# Live-profile pairing roots, cached on (home, ``profiles/`` dir mtime) with a TTL. The liveness
# probe costs ~14 stats per profile; on the 2 s tick that was the watcher's share of the idle
# profile-tree burn (#114041 §2/§3). The profile SET only moves when a dir is added or removed —
# which bumps the parent's mtime — while a marker/tombstone landing inside one is caught by the TTL.
_PAIRING_ROOTS_TTL_S = 30.0
_pairing_roots_cache: tuple[Path, int | None, float, list] | None = None


def _pairing_roots(home: Path) -> list:
    global _pairing_roots_cache
    profiles_dir = home / "profiles"
    dir_mtime, now = _watcher_mtime_ns(profiles_dir), time.monotonic()
    cached = _pairing_roots_cache
    if cached is not None and cached[0] == home and cached[1] == dir_mtime and now - cached[2] < _PAIRING_ROOTS_TTL_S:
        return cached[3]
    from hermes_constants import named_profile_is_live
    roots = [home / "pairing", home / "platforms" / "pairing"]
    with contextlib.suppress(OSError):
        for profile_dir in profiles_dir.iterdir():
            if named_profile_is_live(profile_dir):
                roots += [profile_dir / "pairing", profile_dir / "platforms" / "pairing"]
    _pairing_roots_cache = (home, dir_mtime, now, roots)
    return roots


# Newest outbox-envelope mtime EVER seen (monotone): a drain empties the outbox,
# and falling back to None would fire a spurious pending event after every drain.
_bot_relay_outbox_seen = 0


def _bot_relay_outbox_sig():
    """Newest mtime across pending bot-relay outbox envelopes (monotone). Written by the AGENT
    process, so the files are the only shared signal; the Desktop reacts with a debounced drain.

    Envelopes are written by the AGENT process (``message_agent`` → ``tools.bot_relay.enqueue_envelope``) —
    a different process that never touches this gateway's transports — so the files are the only shared
    signal, exactly like the pairing store. See #92760, #93091.
    """
    global _bot_relay_outbox_seen
    home = _watcher_home()
    root = home.parent.parent if home.parent.name == "profiles" else home
    with contextlib.suppress(OSError):
        for entry in (root / "bot_relay" / "outbox").iterdir():
            if entry.name.endswith(".json"):
                _bot_relay_outbox_seen = max(_bot_relay_outbox_seen, _watcher_mtime_ns(entry) or 0)
    return _bot_relay_outbox_seen or None


# event → (check interval, signature fn, payload fn). Signatures are stat-cheap; the interval
# keeps pricier probes (pet resolves the sheet off disk) off the 0.5s tick. cron/jobs.json
# moves on edits AND scheduler ticks; gateway_state.json is where the messaging gateway
# persists platform connect/disconnect/health (the Messaging page's status signal).
_CHANGE_WATCHES: dict[str, tuple[float, Any, Any]] = {
    "pet.changed": (2.0, _pet_sig, _pet_changed_payload),
    "cron.changed": (1.0, lambda: _home_mtime_ns("cron", "jobs.json"), lambda: {}),
    "sessions.changed": (0.5, _sessions_sig, lambda: {}),
    # Projects created/switched by CLI or agent tooling write projects.db without any
    # state.db movement, so sessions.changed never fires and the desktop Projects
    # sidebar goes stale until a manual refresh (#56757).
    "platforms.changed": (2.0, lambda: _home_mtime_ns("gateway_state.json"), lambda: {}),
    "projects.changed": (2.0, _projects_sig, lambda: {}),
    "pairing.changed": (2.0, _pairing_sig, lambda: {}),
    # 1s so a queued DM envelope reaches the Desktop's push-triggered drain fast.
    "bot_relay.outbox.pending": (1.0, _bot_relay_outbox_sig, lambda: {})}

# state.db moves on every append of a streaming turn and gateway_state.json on
# in-flight bookkeeping; the floor coalesces bursts to one broadcast per window,
# trailing edge included (a floored change keeps its old signature, re-fires later).
_CHANGE_BROADCAST_FLOOR_S = {"sessions.changed": 2.0, "platforms.changed": 5.0}

_change_sigs: dict[str, Any] = {}
_change_checked_at: dict[str, float] = {}
_change_broadcast_at: dict[str, float] = {}


def _broadcast_watched_changes(now: float | None = None) -> None:
    """One pass: recompute due signatures, broadcast events whose signature moved.
    First sighting seeds silently so a gateway boot never fires a refresh storm."""
    from tui_gateway import session_change_cursor
    now = time.monotonic() if now is None else now
    for event, (interval, sig_fn, payload_fn) in _CHANGE_WATCHES.items():
        if event == "sessions.changed":
            interval = session_change_cursor.watch_interval(interval)
        if now - _change_checked_at.get(event, -interval) < interval:
            continue
        _change_checked_at[event] = now
        try:
            sig = sig_fn()
        except Exception:  # noqa: BLE001 - a broken probe must not kill the loop
            continue
        if event not in _change_sigs:
            _change_sigs[event] = sig
            continue
        floor = _CHANGE_BROADCAST_FLOOR_S.get(event, 0.0)
        if sig == _change_sigs[event]:
            continue
        if floor and now - _change_broadcast_at.get(event, -floor) < floor:
            continue  # floored: old signature stays so it re-fires when the window opens
        _change_sigs[event] = sig
        _change_broadcast_at[event] = now
        with contextlib.suppress(Exception):
            _broadcast_global_event(event, payload_fn())
    with _live_transports_lock:
        live = list(_live_transports)
    session_change_cursor.pump(_send_session_cursor_frames, live, now)


def _send_session_cursor_frames(targets: list, payload: dict) -> None:
    """One per-scope ``sessions.changed`` cursor frame to every cursor-aware client."""
    frame = _event_frame("sessions.changed", "", payload)
    for transport in targets:
        try:
            transport.write(frame)
        except Exception:  # one wedged peer must not stall the rest; disconnect teardown unregisters it
            logger.debug("sessions.changed cursor frame write failed", exc_info=True)


_skin_watcher_started = False


def _ensure_skin_watcher() -> None:
    """Start the process's one change watcher (named for its original skin-only duty): cheap
    on-disk signatures → broadcast events, so changes go live without client polling. Idempotent."""
    global _skin_watcher_started
    if _skin_watcher_started:
        return
    _skin_watcher_started = True
    _note_skin_broadcast()  # seed the baseline so only a real change repaints

    def _loop() -> None:
        from tui_gateway import session_change_cursor
        while True:
            # 0.25 s while a cursor-aware client listens (its ≤250 ms window), else the old 0.5 s.
            time.sleep(session_change_cursor.watch_interval(0.5))
            _broadcast_skin_if_changed()
            _broadcast_watched_changes()
    threading.Thread(target=_loop, name="hermes-change-watcher", daemon=True).start()


def register(server) -> None:
    """Publish this module's helpers + handlers onto ``server``, rebound to its globals."""
    bind_module(globals(), server, skip=("_",))
    from tui_gateway import session_change_cursor  # noqa: F401 - arms its pending-request listener

"""Owner-only, bounded reads of the installation's team-room meeting archive."""
import errno
import json
import os
import re
import stat
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .contracts.team_room import TeamRoomGetParams, TeamRoomListParams, TeamRoomMessage
from .methods_files import _principal

_ID = re.compile(r'\d{4}-\d{2}-\d{2}-(standup|campfire)', re.ASCII)
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_DIRECTORY_ENTRIES = 2048
MAX_MEETING_FILES = 512


class _Message(TeamRoomMessage):
    model_config = ConfigDict(extra='ignore', strict=True)


class _Meeting(BaseModel):
    model_config = ConfigDict(extra='ignore', strict=True)
    kind: Literal['standup', 'campfire']
    started: str
    host: str
    topic: str | None = None
    dry_run: bool = False
    sessions: dict[str, str | None] = Field(default_factory=dict)
    messages: list[_Message]
    kept: dict[str, str | None] = Field(default_factory=dict)


@contextmanager
def _archive():
    if os.name != 'posix':
        raise OSError('Safe archive reads require POSIX directory handles')
    # This is an installation-wide archive, not a selected profile's state.
    directory = Path(os.environ.get('HERMES_TEAM_ROOM_DIR') or  # health: allow HX002 -- authoritative team-room wire contract requires this installation-wide override
                     Path.home() / 'hermes-workspaces/Automation/team-room/meetings').expanduser().resolve(strict=True)
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        yield directory, fd
    finally:
        os.close(fd)


def _read_bytes(directory, archive_fd, meeting_id):
    """Resolve containment, then pin every component without following replacement symlinks."""
    try:
        target = (directory / (meeting_id + '.json')).resolve(strict=True)
        if not target.is_relative_to(directory):
            return None
        parts = target.relative_to(directory).parts
        if not parts:
            return None
        fd = os.open('.', os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=archive_fd)
        try:
            for part in parts[:-1]:
                child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
                os.close(fd)
                fd = child
            file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
            info = os.fstat(file_fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE_BYTES:
                os.close(file_fd)
                return None
            with os.fdopen(file_fd, 'rb') as stream:
                data = stream.read(MAX_FILE_BYTES + 1)
                return data if len(data) <= MAX_FILE_BYTES else None
        finally:
            os.close(fd)
    except OSError as exc:
        if exc.errno in {errno.ENOENT, errno.ENOTDIR, errno.ELOOP}:
            return None
        raise
    except RuntimeError:  # Path.resolve on a symlink loop on older Python versions.
        return None


def _load(directory, archive_fd, meeting_id):
    # Callers validate user ids before even opening the archive; scanned names use the same rule.
    data = _read_bytes(directory, archive_fd, meeting_id)
    if data is None:
        return None
    try:
        meeting = _Meeting.model_validate(json.loads(data))
        started = datetime.fromisoformat(meeting.started)
        if meeting.dry_run or meeting.kind != meeting_id[11:] or started.utcoffset() is None:
            return None
        return meeting, started.timestamp()
    except (ValueError, TypeError, ValidationError, UnicodeError, RecursionError, OverflowError):
        return None


def _metadata(meeting_id, meeting):
    participants = list(meeting.sessions) or list(dict.fromkeys(m.name for m in meeting.messages))
    return {'id': meeting_id, 'kind': meeting.kind, 'date': meeting_id[:10],
            'started': meeting.started, 'host': meeting.host, 'topic': meeting.topic,
            'participants': participants}


def _list(directory, archive_fd, params):
    rows = []
    files = 0
    with os.scandir(archive_fd) as entries:
        for count, entry in enumerate(entries, 1):
            if count > MAX_DIRECTORY_ENTRIES:
                raise OSError('Archive scan limit exceeded')
            if not entry.name.endswith('.json') or not _ID.fullmatch(entry.name[:-5]):
                continue
            files += 1
            if files > MAX_MEETING_FILES:
                # Do not advertise an incomplete page as the end of the archive.
                raise OSError('Archive file limit exceeded')
            meeting_id = entry.name[:-5]
            loaded = _load(directory, archive_fd, meeting_id)
            if loaded is None:
                continue
            meeting, started = loaded
            preview = ' '.join(meeting.messages[0].text.split()) if meeting.messages else ''
            preview = preview if len(preview) <= 140 else preview[:139] + '…'
            rows.append((started, meeting_id, {**_metadata(meeting_id, meeting),
                         'message_count': len(meeting.messages), 'preview': preview}))
    rows.sort(key=lambda row: row[:2], reverse=True)
    if params.before is not None:
        cursor = next((i for i, row in enumerate(rows) if row[1] == params.before), None)
        if cursor is None:
            raise ValueError('Unknown cursor')
        rows = rows[cursor + 1:]
    page = rows[:params.limit]
    return {'meetings': [row[2] for row in page],
            'next_before': page[-1][1] if len(rows) > params.limit else None}


def _get(directory, archive_fd, params):
    loaded = _load(directory, archive_fd, params.id)
    if loaded is None:
        raise ValueError('Unknown meeting')
    meeting, _started = loaded
    metadata = _metadata(params.id, meeting)
    kept = []
    for name in metadata['participants']:
        text = meeting.kept.get(name)
        if text and text.strip() and text.strip().casefold() != 'nothing':
            kept.append({'name': name, 'text': text})
    return {**metadata, 'messages': [m.model_dump() for m in meeting.messages], 'kept': kept}


def _rpc(server, get, rid, body):
    try:
        _principal(server)
    except (PermissionError, server.ProfileUnavailableError):
        return server._err(rid, 4403, 'Team room owner scope denied')
    try:
        if get:
            params = TeamRoomGetParams.model_validate(body)
            meeting_id = params.id
        else:
            params = TeamRoomListParams.model_validate(body)
            meeting_id = params.before
        if meeting_id is not None and not _ID.fullmatch(meeting_id):
            raise ValueError('Invalid id')
    except (ValidationError, ValueError, TypeError):
        return server._err(rid, 4400, 'Invalid team room request')
    try:
        with _archive() as (directory, archive_fd):
            result = (_get if get else _list)(directory, archive_fd, params)
        return server._ok(rid, result)
    except ValueError:
        return server._err(rid, 4400, 'Invalid team room request')
    except (OSError, RuntimeError):
        return server._err(rid, 4401, 'Team room unavailable')


def register(server, register_fn):
    # Even bounded disk IO/JSON parsing must not park the stdio/socket reader.
    server._LONG_HANDLERS = server._LONG_HANDLERS | {'team_room.list', 'team_room.get'}
    for name, get in [('team_room.list', False), ('team_room.get', True)]:
        register_fn(name, lambda rid, params, get=get: _rpc(server, get, rid, params))

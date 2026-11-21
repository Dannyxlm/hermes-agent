"""Shared fixture verbs for the fork's relocated SQLite regression tests."""
from persistence.sqlite._holder import StoreHolder
from persistence.sqlite.authority.coordination import SQLiteCoordinationRepository
from persistence.sqlite.conversation._store import compression, launches, messages, sessions
from persistence.sqlite.housekeeping.maintenance import SQLiteSessionMaintenance

MEDIA_ROOT = '/nonexistent/native-inputs'


class SessionDB(StoreHolder):
    create_session = sessions.create_session
    end_session = sessions.end_session
    reopen_session = sessions.reopen_session
    get_session = sessions.get_session
    session_count = sessions.session_count
    list_sessions_rich = sessions.list_sessions_rich
    set_session_archived = sessions.set_session_archived
    set_session_pinned = sessions.set_session_pinned
    get_compression_family = compression.get_compression_family
    get_compression_lineage = compression.get_compression_lineage
    get_compression_tip = compression.get_compression_tip
    publish_compression_child = compression.publish_compression_child
    archive_and_compact = messages.archive_and_compact
    archive_session_messages = messages.archive_session_messages
    append_message = messages.append_message
    append_messages_batch = messages.append_messages_batch
    get_messages = messages.get_messages
    get_messages_as_conversation = messages.get_messages_as_conversation
    get_resume_conversations = messages.get_resume_conversations
    replace_messages = messages.replace_messages
    record_launch = launches.write_launch
    bind_launch = launches.bind_stored_launch
    finish_launch = launches.finish_stored_launch

    def holder(self):
        return self

    def set_session_title(self, *args, **kwargs):
        from persistence.sqlite.conversation._store.titles import set_session_title
        return set_session_title(self, *args, **kwargs)

    def delete_session(self, sid, **kwargs):
        return bool(sessions.delete_session(self, sid, media_root=MEDIA_ROOT, **kwargs))

    def delete_sessions(self, ids, **kwargs):
        return sessions.delete_sessions(self, ids, media_root=MEDIA_ROOT, **kwargs)[0]

    def prune_sessions(self, older_than_days=90, source=None):
        return SQLiteSessionMaintenance(self).prune_sessions(older_than_days, source, media_root=MEDIA_ROOT).count

    def try_acquire_session_turn_lease(self, sid, holder):
        return self._coordination().try_acquire_turn_lease(session_id=sid, holder=holder)

    def refresh_session_turn_lease(self, sid, holder):
        return self._coordination().refresh_turn_lease(session_id=sid, holder=holder)

    def release_session_turn_lease(self, sid, holder):
        return self._coordination().release_turn_lease(session_id=sid, holder=holder)

    def try_acquire_compression_lock(self, sid, holder):
        return self._coordination().try_acquire_compression_lock(session_id=sid, holder=holder)

    def _coordination(self):
        # The repository constructor does not touch the store; these verbs use .holder().
        return SQLiteCoordinationRepository(self)

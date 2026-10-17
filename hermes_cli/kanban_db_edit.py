"""Card field edits and recovery-result backfills."""
import json
import sqlite3
from typing import Optional


def edit_task(
    conn: sqlite3.Connection, task_id: str, *, title: Optional[str] = None,
    body: Optional[str] = None, priority: Optional[int] = None,
    result: Optional[str] = None, summary: Optional[str] = None,
    metadata: Optional[dict] = None, board: Optional[str] = None, resource_class: Optional[str] = None,
) -> bool:
    """Edit fields. resource_class="" clears the override for the next dispatch."""
    from hermes_cli.kanban_db import (
        write_txn, _task_status, _append_event, _synthesize_ended_run,
        _first_line, notify_task_updated,
    )
    from hermes_cli.kanban_resources import validate_resource_class, merge_run_resources

    validate_resource_class(resource_class)
    fields = (("title", title), ("body", body), ("priority", priority), ("resource_class", resource_class))
    changed_fields = [
        field for field, value in fields
        if value is not None
    ]
    with write_txn(conn):
        status = _task_status(conn, task_id)
        if status is None or (result is not None and status != "done"):
            return False
        assignments = []
        params = []
        for field, value in fields:
            if value is not None:
                assignments.append(f"{field} = ?")
                params.append(value)
        if result is not None:
            assignments.append("result = ?")
            params.append(result)
            changed_fields.append("result")
        if not assignments:
            return False
        conn.execute(
            f"UPDATE tasks SET {', '.join(assignments)} WHERE id = ?",
            (*params, task_id),
        )
        if priority is not None:
            _append_event(conn, task_id, "reprioritized", {"priority": priority})
        if result is None:
            non_priority_fields = [field for field in changed_fields if field != "priority"]
            if non_priority_fields:
                _append_event(conn, task_id, "edited", {"fields": non_priority_fields})
        else:
            handoff_summary = summary if summary is not None else result
            changed_fields.append("summary")
            if metadata is not None:
                changed_fields.append("metadata")
            run = conn.execute(
            """
            SELECT id FROM task_runs
             WHERE task_id = ?
               AND outcome = 'completed'
             ORDER BY COALESCE(ended_at, started_at, 0) DESC, id DESC
             LIMIT 1
            """,
            (task_id,),
        ).fetchone()
            if run is None:
                run_id = _synthesize_ended_run(
                    conn, task_id, outcome="completed", summary=handoff_summary, metadata=metadata,
                )
            else:
                run_id = int(run["id"])
                conn.execute("UPDATE task_runs SET summary = ? WHERE id = ?", (handoff_summary, run_id))
                if metadata is not None:
                    metadata = merge_run_resources(conn, run_id, metadata)
                    conn.execute(
                        "UPDATE task_runs SET metadata = ? WHERE id = ?",
                        (json.dumps(metadata, ensure_ascii=False), run_id),
                    )
            _append_event(
                conn, task_id, "edited",
                {
                    "fields": ["result", "summary"] + (["metadata"] if metadata is not None else []),
                    "result_len": len(result) if result else 0,
                    "summary": _first_line(handoff_summary, 400) or None,
                },
                run_id=run_id,
            )
    notify_task_updated(conn, task_id, changed_fields, board=board)
    return True

"""Display-only notification identity for the accepted turn's stream header."""


def message_start_payload(display_kind: str | None, metadata: dict | None) -> dict | None:
    if not display_kind or display_kind == "hidden":
        return None
    fields = ("display_text", "delegation_id", "process_ids", "task_count",
              "completed_count", "failed_count", "duration_seconds")
    metadata = metadata or {}
    return {"presentation": {"display_kind": display_kind,
                             **{key: metadata[key] for key in fields if key in metadata}}}

"""An exact GET capability handler, not an authentication bypass or token provider."""
import sqlite3

from starlette.concurrency import run_in_threadpool
from starlette.responses import JSONResponse

SNAPSHOT_PATH = "/api/mobile/widgets/snapshot"


async def widget_snapshot_response(request, *, service=None):
    if request.method != "GET" or request.url.path != SNAPSHOT_PATH:
        return None
    authorization = request.headers.get("authorization", "")
    token = authorization[7:] if authorization.startswith("Bearer ") else ""
    # Never forward this credential to a general-purpose authentication provider.
    if service is None:
        from tui_gateway import server
        service = server._mobile_push_service()
    headers = {"Cache-Control": "no-store"}
    if service is None:
        return JSONResponse({"detail": "Unavailable"}, status_code=503, headers=headers)
    try:
        snapshot = await run_in_threadpool(service.store.widget_snapshot, token)
    except PermissionError:
        return JSONResponse({"detail": "Unauthorized"}, status_code=401, headers=headers)
    except (OSError, RuntimeError, sqlite3.Error):
        return JSONResponse({"detail": "Unavailable"}, status_code=503, headers=headers)
    return JSONResponse(snapshot, headers=headers)

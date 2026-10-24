"""Serve the checked-in dashboard plugin for local browser acceptance."""
from __future__ import annotations

import argparse
import importlib.util
import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
import uvicorn


HERE = Path(__file__).resolve().parent
PLUGIN = Path(__file__).resolve().parents[2] / "dashboard"


def load_api():
    spec = importlib.util.spec_from_file_location("duty_board_harness_api", PLUGIN / "plugin_api.py")
    if spec is None or spec.loader is None:
        raise RuntimeError("could not load dashboard/plugin_api.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def create_app():
    module = load_api()
    app = FastAPI()
    app.include_router(module.router, prefix="/api/plugins/duty-board")
    app.mount("/plugin", StaticFiles(directory=PLUGIN / "dist"), name="plugin")

    @app.get("/")
    def index():
        return FileResponse(HERE / "index.html")

    @app.get("/sdk.js")
    def sdk():
        return FileResponse(HERE / "sdk.js")

    @app.get("/theme.css")
    def theme():
        return FileResponse(HERE / "theme.css")

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve the Duty Board plugin locally")
    parser.add_argument("--port", type=int, default=9188)
    parser.add_argument("--fixture", type=Path, help="synthetic Hermes home to expose as HERMES_HOME")
    args = parser.parse_args()
    if args.fixture:
        os.environ["HERMES_HOME"] = str(args.fixture.expanduser().resolve())
    uvicorn.run(create_app(), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()

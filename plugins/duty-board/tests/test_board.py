"""Core parity and both loader shapes, using only fictional data."""
import builtins
import importlib.util
import json
import sys
from pathlib import Path

from .fixtures.make_home import add_message, add_session

ROOT = Path(__file__).resolve().parents[1]


def load_plugin(name="duty_test_package"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "__init__.py", submodule_search_locations=[str(ROOT)])
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_core_parity(client, api_module, home, monkeypatch):
    now = float((home / "fixture-now").read_text(encoding="utf-8-sig"))
    add_session(home, "parity", now=now)
    add_message(home, "parity", "assistant", "May I continue?", timestamp=now - 300)
    monkeypatch.setattr(api_module.board.time, "time", lambda: now)
    response = client.get("/api/plugins/duty-board/summary")
    expected = api_module.board.build(now)
    assert response.json() == expected
    # Pin serialized bytes too, not just an equivalent set of values.
    assert response.content == json.dumps(expected, ensure_ascii=False, separators=(",", ":")).encode()


def test_package_core_without_web_dependencies(home, monkeypatch):
    real_import = builtins.__import__

    def without_web(name, *args, **kwargs):
        if name.split(".")[0] in {"fastapi", "pydantic"}:
            raise AssertionError("command path imported a web dependency")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", without_web)
    package = load_plugin("duty_no_web")
    board = importlib.import_module(package.__name__ + ".dashboard.board")
    assert board.build(float((home / "fixture-now").read_text(encoding="utf-8-sig")))["schema_version"] == 2
    from types import SimpleNamespace
    recorded = []
    package.register(SimpleNamespace(register_command=lambda name, handler, **kw: recorded.append(handler), register_cli_command=lambda *a, **kw: None))
    assert len(recorded) == 1
    assert recorded[0]("lang=en") == "Nothing needs you right now."


def test_core_reads_do_not_create_directories(home, api_module):
    import shutil
    shutil.rmtree(home / "duty-board")
    api_module.board.build(float((home / "fixture-now").read_text(encoding="utf-8-sig")))
    assert not (home / "duty-board").exists()

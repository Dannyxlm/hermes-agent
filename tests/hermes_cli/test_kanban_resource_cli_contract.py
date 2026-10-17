"""Named resource classes survive the public create/edit/show CLI boundary."""
import json
from pathlib import Path

from hermes_cli import kanban as cli
from hermes_cli import kanban_db as kb


def test_resource_class_is_a_card_override(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    output = cli.run_slash('create "large build" --resource-class heavy --json')
    assert output.startswith("{"), output
    card = json.loads(output)
    assert card["resource_class"] == "heavy"
    assert "Edited" in cli.run_slash(f'edit {card["id"]} --resource-class standard')
    shown = json.loads(cli.run_slash(f'show {card["id"]} --json'))
    assert shown["task"]["resource_class"] == "standard"

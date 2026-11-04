"""A teammate's display model comes from its selected config backend, never a stale file."""
from hermes_cli import config_backend
from tools.thread_tool import _profile_model


def test_profile_model_uses_readonly_backend_without_local_config(tmp_path, monkeypatch):
    paths = []

    def read(path):
        paths.append(path)
        return {'model': {'default': 'fixture-model', 'provider': 'fixture-provider'}}

    monkeypatch.setattr(config_backend, 'read_config_doc_readonly', read)
    assert _profile_model(tmp_path) == {'model': 'fixture-model', 'provider': 'fixture-provider'}
    assert paths == [tmp_path / 'config.yaml']


def test_unavailable_backend_exposes_no_teammate_model(tmp_path, monkeypatch):
    def unavailable(path):
        raise config_backend.ConfigBackendUnavailable('synthetic offline backend')

    monkeypatch.setattr(config_backend, 'read_config_doc_readonly', unavailable)
    assert _profile_model(tmp_path) == {}

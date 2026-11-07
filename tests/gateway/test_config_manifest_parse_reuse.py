"""Status platform discovery must not reparse unchanged bundled manifests."""
import os

from gateway import config


def test_manifest_name_reuses_parse_and_reads_edits_on_next_call(tmp_path, monkeypatch):
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text("name: poll-cache-fixture-a\n")
    reads = []
    original = config.fast_safe_load

    def counted(text):
        reads.append(text)
        return original(text)

    monkeypatch.setattr(config, "fast_safe_load", counted)
    assert config._bundled_platform_manifest_name(tmp_path) == "poll-cache-fixture-a"
    assert config._bundled_platform_manifest_name(tmp_path) == "poll-cache-fixture-a"
    assert len(reads) == 1, "unchanged status discovery must not reparse bundled YAML"
    before = manifest.stat()
    manifest.write_text("name: poll-cache-fixture-b\n")
    os.utime(manifest, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert config._bundled_platform_manifest_name(tmp_path) == "poll-cache-fixture-b"
    manifest.unlink()
    assert config._bundled_platform_manifest_name(tmp_path) is None
    (tmp_path / "plugin.yml").write_text("name: poll-cache-fixture-c\n")
    assert config._bundled_platform_manifest_name(tmp_path) == "poll-cache-fixture-c"


def test_large_or_invalid_manifest_is_not_a_cached_success(tmp_path):
    manifest = tmp_path / "plugin.yaml"
    manifest.write_text("name: large-fixture\n#" + "x" * 70000)
    assert config._bundled_platform_manifest_name(tmp_path) == "large-fixture"
    manifest.write_text("name: [invalid\n")
    assert config._bundled_platform_manifest_name(tmp_path) is None
    manifest.write_text("name: recovered-fixture\n")
    assert config._bundled_platform_manifest_name(tmp_path) == "recovered-fixture"
